"""Queue helpers. The queue is just the ``jobs`` table; a single worker claims
the next queued job and runs it to completion. Company work (download/recheck)
is claimed ahead of the long background ``enumerate``; within a priority it's
FIFO, so companies still download one at a time in order."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import case, select
from sqlalchemy.orm import Session

from .models import (
    JOB_DONE,
    JOB_FAILED,
    JOB_QUEUED,
    JOB_RUNNING,
    KIND_DOWNLOAD,
    KIND_ENUMERATE,
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
) -> Job:
    """Queue a download (or recheck) for a company, unless one is already active.

    ``max_batches`` caps how many 30-doc batches to pull (test mode); None means
    use the global default (``settings.default_max_batches``, also possibly None).
    """
    existing = _active_job_for_company(db, company.id)
    if existing:
        return existing
    params = json.dumps({"max_batches": max_batches}) if max_batches is not None else None
    job = Job(kind=kind, company_id=company.id, status=JOB_QUEUED, params=params)
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def enqueue_recheck(db: Session, company: Company, max_batches: int | None = None) -> Job:
    return enqueue_download(db, company, kind=KIND_RECHECK, max_batches=max_batches)


def enqueue_enumerate(db: Session, profile_type: str = "Company",
                      max_pages: int | None = None) -> Job:
    job = Job(
        kind=KIND_ENUMERATE,
        status=JOB_QUEUED,
        params=json.dumps({"profile_type": profile_type, "max_pages": max_pages}),
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


def claim_next_job(db: Session) -> Job | None:
    """Atomically take the next queued job and mark it running.

    Ordering: company work (download/recheck/probe) is claimed ahead of the
    long-running catalog ``enumerate`` so an in-progress enumerate can't block a
    user's download -- it simply resumes (idempotently) once downloads drain.
    Within the same priority it's FIFO by creation time, so downloads still run
    one company at a time in order.
    """
    enumerate_last = case((Job.kind == KIND_ENUMERATE, 1), else_=0)
    job = db.scalar(
        select(Job)
        .where(Job.status == JOB_QUEUED)
        .order_by(enumerate_last.asc(), Job.created_at.asc())
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
