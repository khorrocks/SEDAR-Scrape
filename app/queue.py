"""Queue helpers. The queue is just the ``jobs`` table; a single worker claims
the next queued job and runs it to completion. Company work (download/recheck)
is claimed ahead of the long background ``enumerate``; within a priority it's
FIFO, so companies still download one at a time in order."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import case, or_, select
from sqlalchemy.orm import Session

from .models import (
    JOB_DONE,
    JOB_FAILED,
    JOB_PAUSED,
    JOB_QUEUED,
    JOB_RUNNING,
    KIND_DOWNLOAD,
    KIND_ENUMERATE,
    KIND_PROBE,
    KIND_RECHECK,
    Company,
    Job,
)


def _active_job_for_company(db: Session, company_id: int) -> Job | None:
    return db.scalar(
        select(Job).where(
            Job.company_id == company_id,
            Job.status.in_([JOB_QUEUED, JOB_RUNNING]),
        )
    )


def enqueue_download(
    db: Session,
    company: Company,
    kind: str = KIND_DOWNLOAD,
    max_batches: int | None = None,
    attempt: int = 1,
) -> Job | None:
    """Queue a download (or recheck) for a company, unless one is already active.

    ``max_batches`` caps how many 30-doc batches to pull (test mode); None means
    use the global default (``settings.default_max_batches``, also possibly None).
    """
    # A paused company accepts no work at all -- not from the UI, the cron, or an
    # automatic retry. Callers must handle None.
    if company.paused:
        return None
    existing = _active_job_for_company(db, company.id)
    if existing:
        return existing
    payload: dict = {}
    if max_batches is not None:
        payload["max_batches"] = max_batches
    if attempt > 1:
        payload["attempt"] = attempt
    params = json.dumps(payload) if payload else None
    job = Job(kind=kind, company_id=company.id, status=JOB_QUEUED, params=params)
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def enqueue_recheck(
    db: Session, company: Company, max_batches: int | None = None
) -> Job | None:
    return enqueue_download(db, company, kind=KIND_RECHECK, max_batches=max_batches)


def auto_retry(db: Session, job: Job | None, max_attempts: int) -> Job | None:
    """Put a failed company download back in the queue automatically.

    Bot walls, browser wedges and download timeouts are routine against SEDAR+,
    and a company that stops short is nearly always finishable on a later pass --
    it resumes from its checkpoint and re-downloads nothing. Leaving that to a
    human meant a partially-collected company sat untouched until someone
    noticed. Retries are capped so a genuinely broken company can't cycle
    forever, and it goes to the BACK of the queue so one bad company cannot
    starve the others.
    """
    if job is None or job.company_id is None:
        return None
    if job.kind not in (KIND_DOWNLOAD, KIND_RECHECK):
        return None
    params = json.loads(job.params or "{}")
    attempt = int(params.get("attempt") or 1)
    if attempt >= max_attempts:
        return None
    company = db.get(Company, job.company_id)
    if company is None:
        return None
    return enqueue_download(
        db,
        company,
        kind=job.kind,
        max_batches=params.get("max_batches"),
        attempt=attempt + 1,
    )


def enqueue_enumerate(db: Session, profile_type: str = "Company",
                      max_pages: int | None = None, start_page: int = 0) -> Job:
    # ``start_page`` seeds batches_done so the worker fast-forwards to it (resume
    # a paused/interrupted enumerate instead of re-walking from the top).
    job = Job(
        kind=KIND_ENUMERATE,
        status=JOB_QUEUED,
        params=json.dumps({"profile_type": profile_type, "max_pages": max_pages}),
        batches_done=start_page or 0,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def has_pending_company_job(db: Session) -> bool:
    """True if a download/recheck job is waiting in the queue. A long-running
    enumerate calls this between pages so it can pause (checkpoint + requeue
    itself) and let the higher-priority company work run first."""
    return db.scalar(
        select(Job.id)
        .where(Job.status == JOB_QUEUED, Job.kind.in_([KIND_DOWNLOAD, KIND_RECHECK]))
        .limit(1)
    ) is not None


def is_pause_requested(db: Session, job_id: int) -> bool:
    """Fresh read of a job's pause flag (set by the API from another session)."""
    return bool(db.scalar(select(Job.pause_requested).where(Job.id == job_id)))


def claim_next_job(db: Session) -> Job | None:
    """Atomically take the next queued job and mark it running.

    Ordering by priority: debug ``probe`` first (a diagnostic should not sit
    behind a stuck download), then company work (download/recheck), then the
    long-running catalog ``enumerate`` last so it can't block a user's download
    (it resumes idempotently once downloads drain). Within a priority it's FIFO
    by creation time, so downloads still run one company at a time in order.
    """
    priority = case(
        (Job.kind == KIND_PROBE, 0),
        (Job.kind == KIND_ENUMERATE, 2),
        else_=1,
    )
    # Never claim work for a paused company, even if the job predates the pause.
    # Enforcing it here as well as at enqueue is what makes the pause survive
    # anything already sitting in the queue.
    job = db.scalar(
        select(Job)
        .outerjoin(Company, Job.company_id == Company.id)
        .where(
            Job.status == JOB_QUEUED,
            or_(Job.company_id.is_(None), Company.paused.is_(False)),
        )
        .order_by(priority.asc(), Job.created_at.asc())
        .limit(1)
    )
    if not job:
        return None
    job.status = JOB_RUNNING
    job.started_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(job)
    return job


def finish_job(db: Session, job: Job, *, ok: bool, error: str | None = None) -> None:
    # A job the worker parked as 'paused' (manual pause) must not be flipped to
    # done/failed by the run loop; leave it so it can be resumed.
    if job.status == JOB_PAUSED:
        return
    job.status = JOB_DONE if ok else JOB_FAILED
    job.error = error
    job.blocked = False
    job.finished_at = datetime.now(timezone.utc)
    db.commit()


def requeue_stuck_jobs(db: Session) -> int:
    """On worker startup, any job left 'running' (from a crash/redeploy) is
    reset to queued so it gets picked up again."""
    stuck = list(db.scalars(select(Job).where(Job.status == JOB_RUNNING)))
    for j in stuck:
        j.status = JOB_QUEUED
        j.started_at = None
        j.blocked = False
    db.commit()
    return len(stuck)
