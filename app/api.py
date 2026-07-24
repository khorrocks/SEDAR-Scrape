"""REST API. The web process only reads/writes the database and enqueues jobs;
it never launches Chrome (that is the worker's job)."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import FileResponse, RedirectResponse
from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session

from . import r2
from .config import settings
from .db import get_db
from .models import (
    JOB_DONE,
    JOB_FAILED,
    JOB_PAUSED,
    JOB_QUEUED,
    JOB_RUNNING,
    KIND_ENUMERATE,
    Company,
    Document,
    Job,
)
from . import queue as q
from .schemas import (
    AddCompanyRequest,
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
    """Queue a (browser) job that populates the autocomplete catalog. If one is
    already queued or running, return it rather than stacking another full run."""
    existing = db.scalar(
        select(Job).where(
            Job.kind == KIND_ENUMERATE, Job.status.in_([JOB_QUEUED, JOB_RUNNING])
        )
    )
    if existing:
        return _job_out(existing)
    # Resuming supersedes any manually-paused enumerate (a fresh run re-walks
    # from the top idempotently), so retire paused ones to keep the queue clean.
    for p in db.scalars(select(Job).where(Job.kind == KIND_ENUMERATE, Job.status == JOB_PAUSED)):
        p.status = JOB_DONE
        p.message = "resumed"
    job = q.enqueue_enumerate(db, profile_type=req.profile_type, max_pages=req.max_pages)
    return _job_out(job)


@router.post("/jobs/{job_id}/pause", response_model=JobOut)
def pause_job(job_id: int, db: Session = Depends(get_db)):
    """Ask a running enumerate to pause at its next page checkpoint, freeing the
    worker for other jobs. Resume by launching enumerate again."""
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    if job.kind != KIND_ENUMERATE or job.status != JOB_RUNNING:
        raise HTTPException(409, "only a running enumerate can be paused")
    job.pause_requested = True
    job.message = "pausing at the next page…"
    db.commit()
    return _job_out(job)


# --------------------------------------------------------------------------- #
# Saved companies + downloads
# --------------------------------------------------------------------------- #
@router.post("/companies/add", response_model=JobOut | CompanyOut)
def add_company(req: AddCompanyRequest, db: Session = Depends(get_db)):
    """Add a company by SEDAR issuer number (no enumeration needed) and queue a
    download. Upserts on number so re-adding is safe."""
    number = (req.number or "").strip()
    if not number:
        raise HTTPException(400, "a SEDAR issuer number is required")
    company = db.scalar(select(Company).where(Company.number == number))
    if company is None:
        company = Company(number=number, name=(req.name or number).strip(), type="(added)")
        db.add(company)
    elif req.name:
        company.name = req.name.strip()
    if req.exchange is not None:
        company.exchange = req.exchange.strip() or None
    if req.ticker is not None:
        company.ticker = req.ticker.strip() or None
    company.saved = True
    db.commit()
    db.refresh(company)
    if req.download:
        return _job_out(q.enqueue_download(db, company, max_batches=req.max_batches))
    return CompanyOut.model_validate(company)


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
    if req.exchange is not None:
        company.exchange = req.exchange.strip() or None
    if req.ticker is not None:
        company.ticker = req.ticker.strip() or None
    company.saved = True
    db.commit()
    if req.download:
        job = q.enqueue_download(db, company, max_batches=req.max_batches)
        return _job_out(job)
    return CompanyOut.model_validate(company)


@router.delete("/companies/{company_id}/save", response_model=CompanyOut)
def unsave_company(company_id: int, db: Session = Depends(get_db)):
    company = _get_company(db, company_id)
    company.saved = False
    db.commit()
    return CompanyOut.model_validate(company)


@router.post("/companies/{company_id}/download", response_model=JobOut)
def download_company(
    company_id: int,
    max_batches: int | None = Query(None, description="test mode: cap 30-doc batches"),
    db: Session = Depends(get_db),
):
    company = _get_company(db, company_id)
    job = q.enqueue_download(db, company, max_batches=max_batches)
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
    statuses = [JOB_QUEUED, JOB_RUNNING, JOB_PAUSED]
    if include_finished:
        statuses += [JOB_DONE, JOB_FAILED]
    stmt = (
        select(Job)
        .where(Job.status.in_(statuses))
        .order_by(Job.created_at.desc())
        .limit(limit)
    )
    jobs = list(db.scalars(stmt))
    # Stable ordering: active (queued/running/paused) by FIFO, then finished by recency.
    active = sorted(
        [j for j in jobs if j.status in (JOB_QUEUED, JOB_RUNNING, JOB_PAUSED)],
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
    """Serve a downloaded zip. ``path`` is a Document.batch_zip value: either a
    data-dir-relative local path (local mode) or an absolute R2 object key
    (R2 mode). We try local disk first, then redirect to a presigned R2 URL."""
    base = settings.data_dir.resolve()
    target = (base / path).resolve()
    if (base in target.parents or target == base) and target.is_file():
        return FileResponse(target, filename=target.name, media_type="application/zip")

    # Not on local disk: if it's an R2 key under our root prefix, presign it.
    if settings.r2_enabled and r2.to_relative(path) is not None:
        return RedirectResponse(r2.presigned_url(path))

    raise HTTPException(404, "file not found")


# --------------------------------------------------------------------------- #
# R2 viewer (read-only browse of the object store, rooted at <bucket>/<prefix>)
# --------------------------------------------------------------------------- #
@router.get("/r2/status")
def r2_status():
    """Whether R2 is configured, plus the root the viewer is anchored at."""
    return {
        "enabled": settings.r2_enabled,
        "bucket": settings.r2_bucket if settings.r2_enabled else None,
        "prefix": settings.r2_root_prefix if settings.r2_enabled else None,
    }


@router.get("/r2/list")
def r2_list(path: str = Query("", description="folder path relative to the root prefix")):
    """List immediate subfolders + files under the given path. Read-only."""
    if not settings.r2_enabled:
        raise HTTPException(503, "R2 is not configured")
    try:
        return r2.list_dir(path)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get("/r2/object")
def r2_object(path: str = Query(..., description="object path relative to the root prefix")):
    """Redirect to a short-lived presigned URL for one object. Read-only."""
    if not settings.r2_enabled:
        raise HTTPException(503, "R2 is not configured")
    try:
        key = r2.full_key(path)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not r2.object_exists(key):
        raise HTTPException(404, "object not found")
    return RedirectResponse(r2.presigned_url(key))


@router.post("/debug/probe", response_model=JobOut)
def debug_probe(payload: dict, db: Session = Depends(get_db)):
    """Queue a debug probe that loads the given URLs in the live browser and
    reports titles/links. Body: {"urls": ["https://..."]}."""
    import json as _json

    from .models import KIND_PROBE

    job = Job(kind=KIND_PROBE, status=JOB_QUEUED, params=_json.dumps(payload))
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


# --------------------------------------------------------------------------- #
# Admin (destructive; gated behind ADMIN_TOKEN)
# --------------------------------------------------------------------------- #
@router.post("/admin/reset")
def admin_reset(
    x_admin_token: str | None = Header(default=None),
    db: Session = Depends(get_db),
):
    """Wipe the app's database state — jobs (queue), documents, and companies
    (saved + catalog) — for a fresh start. Does NOT touch R2 objects.

    Disabled unless ADMIN_TOKEN is set; the request must send a matching
    X-Admin-Token header.
    """
    if not settings.admin_token:
        raise HTTPException(403, "admin reset is disabled (set ADMIN_TOKEN to enable)")
    if x_admin_token != settings.admin_token:
        raise HTTPException(401, "invalid admin token")
    # Delete children before parents to satisfy foreign keys.
    n_docs = db.execute(delete(Document)).rowcount
    n_jobs = db.execute(delete(Job)).rowcount
    n_companies = db.execute(delete(Company)).rowcount
    db.commit()
    return {
        "reset": True,
        "deleted": {"documents": n_docs, "jobs": n_jobs, "companies": n_companies},
        "note": "R2 objects were not touched",
    }
