"""REST API. The web process only reads/writes the database and enqueues jobs;
it never launches Chrome (that is the worker's job)."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from .config import settings
from .db import get_db
from .models import (
    JOB_DONE,
    JOB_FAILED,
    JOB_QUEUED,
    JOB_RUNNING,
    Company,
    Document,
    Job,
)
from . import queue as q
from .schemas import (
    CompanyOut,
    DocumentOut,
    EnumerateRequest,
    JobOut,
    SaveRequest,
)

router = APIRouter(prefix="/api")


def _job_out(job: Job) -> JobOut:
    out = JobOut.model_validate(job)
    out.company_name = job.company.name if job.company else None
    return out


# --------------------------------------------------------------------------- #
# Catalog search / autocomplete
# --------------------------------------------------------------------------- #
@router.get("/companies/search", response_model=list[CompanyOut])
def search_companies(
    q_: str = Query("", alias="q"),
    limit: int = Query(10, le=50),
    db: Session = Depends(get_db),
):
    """Local autocomplete over the enumerated catalog (name or number).

    Deliberately hits our own DB, not SEDAR+ live -- fast, and it avoids
    hammering the site (which would get us blocked) on every keystroke.
    """
    term = q_.strip()
    stmt = select(Company)
    if term:
        like = f"%{term}%"
        stmt = stmt.where(or_(Company.name.ilike(like), Company.number.ilike(like)))
    # Saved first, then alphabetical.
    stmt = stmt.order_by(Company.saved.desc(), Company.name.asc()).limit(limit)
    return list(db.scalars(stmt))


@router.get("/catalog/stats")
def catalog_stats(db: Session = Depends(get_db)):
    total = db.scalar(select(func.count(Company.id)))
    saved = db.scalar(select(func.count(Company.id)).where(Company.saved.is_(True)))
    docs = db.scalar(select(func.count(Document.id)))
    return {"companies": total or 0, "saved": saved or 0, "documents": docs or 0}


@router.post("/catalog/enumerate", response_model=JobOut)
def enumerate_catalog(req: EnumerateRequest, db: Session = Depends(get_db)):
    """Queue a (browser) job that populates the autocomplete catalog."""
    job = q.enqueue_enumerate(db, profile_type=req.profile_type, max_pages=req.max_pages)
    return _job_out(job)


# --------------------------------------------------------------------------- #
# Saved companies + downloads
# --------------------------------------------------------------------------- #
@router.get("/saved", response_model=list[CompanyOut])
def list_saved(db: Session = Depends(get_db)):
    stmt = select(Company).where(Company.saved.is_(True)).order_by(Company.name.asc())
    return list(db.scalars(stmt))


def _get_company(db: Session, company_id: int) -> Company:
    company = db.get(Company, company_id)
    if company is None:
        raise HTTPException(404, "company not found")
    return company


@router.post("/companies/{company_id}/save", response_model=JobOut | CompanyOut)
def save_company(company_id: int, req: SaveRequest, db: Session = Depends(get_db)):
    company = _get_company(db, company_id)
    company.saved = True
    db.commit()
    if req.download:
        job = q.enqueue_download(db, company)
        return _job_out(job)
    return CompanyOut.model_validate(company)


@router.delete("/companies/{company_id}/save", response_model=CompanyOut)
def unsave_company(company_id: int, db: Session = Depends(get_db)):
    company = _get_company(db, company_id)
    company.saved = False
    db.commit()
    return CompanyOut.model_validate(company)


@router.post("/companies/{company_id}/download", response_model=JobOut)
def download_company(company_id: int, db: Session = Depends(get_db)):
    company = _get_company(db, company_id)
    job = q.enqueue_download(db, company)
    return _job_out(job)


@router.post("/companies/{company_id}/recheck", response_model=JobOut)
def recheck_company(company_id: int, db: Session = Depends(get_db)):
    company = _get_company(db, company_id)
    job = q.enqueue_recheck(db, company)
    return _job_out(job)


@router.get("/companies/{company_id}/documents", response_model=list[DocumentOut])
def company_documents(company_id: int, db: Session = Depends(get_db)):
    _get_company(db, company_id)
    stmt = (
        select(Document)
        .where(Document.company_id == company_id)
        .order_by(Document.downloaded_at.desc(), Document.id.desc())
    )
    return list(db.scalars(stmt))


# --------------------------------------------------------------------------- #
# Queue visualisation
# --------------------------------------------------------------------------- #
@router.get("/queue", response_model=list[JobOut])
def list_queue(
    include_finished: bool = Query(True),
    limit: int = Query(50, le=200),
    db: Session = Depends(get_db),
):
    statuses = [JOB_QUEUED, JOB_RUNNING]
    if include_finished:
        statuses += [JOB_DONE, JOB_FAILED]
    stmt = (
        select(Job)
        .where(Job.status.in_(statuses))
        .order_by(Job.created_at.desc())
        .limit(limit)
    )
    jobs = list(db.scalars(stmt))
    # Stable ordering: active (queued/running) by FIFO, then finished by recency.
    active = sorted(
        [j for j in jobs if j.status in (JOB_QUEUED, JOB_RUNNING)],
        key=lambda j: j.created_at,
    )
    finished = sorted(
        [j for j in jobs if j.status in (JOB_DONE, JOB_FAILED)],
        key=lambda j: j.finished_at or j.created_at,
        reverse=True,
    )
    return [_job_out(j) for j in active + finished]


@router.delete("/jobs/{job_id}", response_model=JobOut)
def cancel_job(job_id: int, db: Session = Depends(get_db)):
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    if job.status != JOB_QUEUED:
        raise HTTPException(409, f"only queued jobs can be cancelled (status={job.status})")
    from .models import JOB_CANCELLED

    job.status = JOB_CANCELLED
    db.commit()
    return _job_out(job)


# --------------------------------------------------------------------------- #
# Files + cron
# --------------------------------------------------------------------------- #
@router.get("/files/download")
def download_file(path: str = Query(...)):
    """Serve a downloaded zip. ``path`` is relative to the data dir; we resolve
    and confirm it stays inside data_dir to prevent traversal."""
    base = settings.data_dir.resolve()
    target = (base / path).resolve()
    if base not in target.parents and target != base:
        raise HTTPException(400, "invalid path")
    if not target.is_file():
        raise HTTPException(404, "file not found")
    return FileResponse(target, filename=target.name, media_type="application/zip")


@router.post("/debug/probe", response_model=JobOut)
def debug_probe(payload: dict, db: Session = Depends(get_db)):
    """Queue a debug probe that loads the given URLs in the live browser and
    reports titles/links. Body: {"urls": ["https://..."]}."""
    import json as _json

    from .models import KIND_PROBE

    job = Job(kind=KIND_PROBE, status=JOB_QUEUED,
              params=_json.dumps({"urls": payload.get("urls", [])}))
    db.add(job)
    db.commit()
    db.refresh(job)
    return _job_out(job)


@router.post("/cron/recheck-all")
def recheck_all(db: Session = Depends(get_db)):
    """Queue a recheck for every saved company. Wire a Railway Cron service to
    POST this daily (or set ENABLE_INPROCESS_CRON=true to do it in-process)."""
    companies = list(db.scalars(select(Company).where(Company.saved.is_(True))))
    jobs = [q.enqueue_recheck(db, c) for c in companies]
    return {"queued": len(jobs), "job_ids": [j.id for j in jobs]}
