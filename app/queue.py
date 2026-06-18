"""Queue helpers. The queue is just the ``jobs`` table; a single worker claims
the oldest queued job and runs it to completion, so ordering == FIFO and only
one company downloads at a time."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import select
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


def enqueue_download(db: Session, company: Company, kind: str = KIND_DOWNLOAD) -> Job:
    """Queue a download (or recheck) for a company, unless one is already active."""
    existing = _active_job_for_company(db, company.id)
    if existing:
        return existing
    job = Job(kind=kind, company_id=company.id, status=JOB_QUEUED)
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def enqueue_recheck(db: Session, company: Company) -> Job:
    return enqueue_download(db, company, kind=KIND_RECHECK)


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


def claim_next_job(db: Session) -> Job | None:
    """Atomically take the oldest queued job and mark it running."""
    job = db.scalar(
        select(Job).where(Job.status == JOB_QUEUED).order_by(Job.created_at.asc()).limit(1)
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
    job.finished_at = datetime.now(timezone.utc)
    db.commit()


def requeue_stuck_jobs(db: Session) -> int:
    """On worker startup, any job left 'running' (from a crash/redeploy) is
    reset to queued so it gets picked up again."""
    stuck = list(db.scalars(select(Job).where(Job.status == JOB_RUNNING)))
    for j in stuck:
        j.status = JOB_QUEUED
        j.started_at = None
    db.commit()
    return len(stuck)
