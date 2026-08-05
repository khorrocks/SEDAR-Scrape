"""REST API. The web process only reads/writes the database and enqueues jobs;
it never launches Chrome (that is the worker's job)."""

from __future__ import annotations

import json as _json
from pathlib import Path

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response
from fastapi.responses import FileResponse, RedirectResponse
from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.orm import Session

from . import r2
from .config import settings
from .db import get_db
from .models import (
    BATCH_SHORT,
    JOB_CANCELLED,
    JOB_DONE,
    JOB_FAILED,
    JOB_PAUSED,
    JOB_QUEUED,
    JOB_RUNNING,
    KIND_DOWNLOAD,
    KIND_ENUMERATE,
    KIND_RECHECK,
    KIND_RESOLVE,
    Batch,
    Company,
    Document,
    Job,
)
from . import queue as q
from .schemas import (
    AddCompanyRequest,
    BulkAddRequest,
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


def _require_job(job: Job | None, company: Company) -> Job:
    """A paused company refuses work; say so plainly rather than 500ing."""
    if job is None:
        raise HTTPException(
            409,
            f"{company.name} is paused — unpause it before queueing work",
        )
    return job


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


def _csv_response(rows: list[list], header: list[str], filename: str):
    """Stream rows as CSV. Values are quoted defensively -- company names contain
    commas, slashes and newlines, and the submitted date is a two-line cell."""
    import csv as _csv
    import io as _io

    buf = _io.StringIO()
    w = _csv.writer(buf, quoting=_csv.QUOTE_ALL)
    w.writerow(header)
    for r in rows:
        w.writerow(["" if v is None else str(v).replace("\r\n", "\n") for v in r])
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/catalog/export")
def export_catalog(
    fmt: str = Query("csv", pattern="^(csv|json)$"),
    db: Session = Depends(get_db),
):
    """The whole enumerated issuer catalog: the name -> SEDAR number join table.

    This lives in the app's own database (not D1/KV), which is why it isn't
    findable from outside. SEDAR+ records jurisdiction, not listing venue, so
    there is no exchange column here -- exchange/ticker are supplied by us per
    company, not by SEDAR.
    """
    stmt = select(Company).order_by(Company.name.asc())
    companies = list(db.scalars(stmt))
    if fmt == "json":
        return [
            {
                "sedar_number": c.number,
                "name": c.name,
                "jurisdiction": c.jurisdiction,
                "profile_type": c.type,
                "in_default": c.in_default,
                "cease_trade_order": c.cease_trade_order,
            }
            for c in companies
        ]
    return _csv_response(
        [
            [c.number, c.name, c.jurisdiction, c.type, c.in_default, c.cease_trade_order]
            for c in companies
        ],
        ["sedar_number", "name", "jurisdiction", "profile_type",
         "in_default", "cease_trade_order"],
        "sedar_catalog.csv",
    )


@router.get("/export/documents")
def export_documents(
    company_id: int | None = Query(None, description="omit for every company"),
    fmt: str = Query("csv", pattern="^(csv|json)$"),
    db: Session = Depends(get_db),
):
    """Every indexed document with its filing date, hash and archive location.

    The submitted date is scraped and stored here but never written to R2 -- the
    archives contain PDFs only -- so a downstream ingest reading R2 alone has no
    filing_date to bind. This exposes it for the documents already held, with no
    re-download. NOTE: `submitted` is the rendered SEDAR+ cell, typically two
    lines ("02 Dec 2024 17:49 EST" + a long form); parse, don't assume ISO.
    """
    stmt = (
        select(Document, Company)
        .join(Company, Document.company_id == Company.id)
        .order_by(Document.company_id.asc(), Document.id.asc())
    )
    if company_id is not None:
        stmt = stmt.where(Document.company_id == company_id)
    pairs = list(db.execute(stmt))
    if fmt == "json":
        return [
            {
                "sedar_number": c.number,
                "company": c.name,
                "folder_slug": c.folder_slug,
                "title": d.title,
                "submitted": d.submitted,
                "jurisdiction": d.jurisdiction,
                "file_size": d.file_size,
                "archive": d.batch_zip,
                "archive_member": d.archive_member,
                "sha256": d.content_sha256,
                "downloaded_at": d.downloaded_at.isoformat() if d.downloaded_at else None,
            }
            for d, c in pairs
        ]
    return _csv_response(
        [
            [c.number, c.name, c.folder_slug, d.title, d.submitted, d.jurisdiction,
             d.file_size, d.batch_zip, d.archive_member, d.content_sha256,
             d.downloaded_at.isoformat() if d.downloaded_at else None]
            for d, c in pairs
        ],
        ["sedar_number", "company", "folder_slug", "title", "submitted",
         "jurisdiction", "file_size", "archive", "archive_member", "sha256",
         "downloaded_at"],
        "sedar_documents.csv",
    )


@router.get("/companies")
def list_companies(
    q_: str = Query("", alias="q"),
    filter_: str = Query("all", alias="filter",
                         pattern="^(all|saved|unsaved|no_number|no_slug|paused|flagged)$"),
    limit: int = Query(100, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """Paginated catalog listing for the manager page."""
    stmt = select(Company)
    term = q_.strip()
    if term:
        like = f"%{term}%"
        stmt = stmt.where(or_(Company.name.ilike(like), Company.number.ilike(like),
                              Company.ticker.ilike(like)))
    if filter_ == "saved":
        stmt = stmt.where(Company.saved.is_(True))
    elif filter_ == "unsaved":
        stmt = stmt.where(Company.saved.is_(False))
    elif filter_ == "no_number":
        stmt = stmt.where(or_(Company.number.is_(None), Company.number == ""))
    elif filter_ == "no_slug":
        stmt = stmt.where(or_(Company.exchange.is_(None), Company.exchange == "",
                              Company.ticker.is_(None), Company.ticker == ""))
    elif filter_ == "paused":
        stmt = stmt.where(Company.paused.is_(True))
    elif filter_ == "flagged":
        stmt = stmt.where(or_(
            and_(Company.in_default.is_not(None), Company.in_default != "",
                 Company.in_default != "No"),
            and_(Company.cease_trade_order.is_not(None), Company.cease_trade_order != "",
                 Company.cease_trade_order != "No"),
        ))
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = list(db.scalars(stmt.order_by(Company.name.asc()).limit(limit).offset(offset)))
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "companies": [CompanyOut.model_validate(c) for c in rows],
    }


@router.post("/companies/bulk-update")
def bulk_update_companies(payload: dict, db: Session = Depends(get_db)):
    """Apply one change to many companies.

    Body: {"ids": [...], "set": {"saved": true, "exchange": "TSXV", ...}}.
    Only whitelisted fields are writable -- a bulk endpoint that can set any
    column is a foot-gun next to counters like total_documents.
    """
    ids = payload.get("ids") or []
    changes = payload.get("set") or {}
    allowed = {"saved", "paused", "exchange", "ticker", "name"}
    unknown = set(changes) - allowed
    if unknown:
        raise HTTPException(400, f"cannot bulk-set: {', '.join(sorted(unknown))}")
    if not ids:
        raise HTTPException(400, "no company ids given")
    updated = 0
    for c in db.scalars(select(Company).where(Company.id.in_(ids))):
        for k, v in changes.items():
            if k in ("exchange", "ticker", "name"):
                v = (v or "").strip() or None
                if k == "name" and not v:
                    continue  # never blank a name
            setattr(c, k, v)
        updated += 1
    db.commit()
    return {"updated": updated}


@router.post("/companies/import")
def import_companies(payload: dict, db: Session = Depends(get_db)):
    """Import mapped CSV rows: [{name, number, exchange, ticker}, ...].

    Matches an existing company by SEDAR number first, then by exact name, so a
    re-import updates rather than duplicating. Rows without a number are still
    created -- /companies/resolve-numbers fills those in afterwards.
    """
    rows = payload.get("rows") or []
    save = bool(payload.get("save", True))
    created, matched, skipped = 0, 0, []
    touched: list[int] = []
    for i, row in enumerate(rows):
        name = (row.get("name") or "").strip()
        number = (row.get("number") or "").strip()
        if not name and not number:
            skipped.append({"row": i + 1, "reason": "no name or number"})
            continue
        company = None
        if number:
            company = db.scalar(select(Company).where(Company.number == number))
        if company is None and name:
            company = db.scalar(select(Company).where(Company.name == name))
        if company is None:
            # NULL, not "": UNIQUE(number) collapses every "" into one value, so
            # storing blanks would reject the second numberless issuer.
            company = Company(number=number or None, name=name or number,
                              type="(imported)")
            db.add(company)
            created += 1
        else:
            matched += 1
            if number and not (company.number or "").strip():
                company.number = number
        for field in ("exchange", "ticker"):
            val = (row.get(field) or "").strip()
            if val:
                setattr(company, field, val)
        if save:
            company.saved = True
        db.flush()
        touched.append(company.id)
    db.commit()
    missing = db.scalar(
        select(func.count(Company.id)).where(
            Company.id.in_(touched), or_(Company.number.is_(None), Company.number == "")
        )
    ) or 0
    return {"created": created, "matched": matched, "skipped": skipped,
            "ids": touched, "missing_numbers": missing, "total": len(rows)}


@router.post("/companies/resolve-numbers", response_model=JobOut)
def resolve_numbers(payload: dict | None = None, db: Session = Depends(get_db)):
    """Queue a browser job that looks up missing SEDAR numbers by company name.

    Body may pass {"ids": [...]}; with none given, every company missing a number
    is attempted. Only exact name matches are written -- near misses come back as
    a review list, because a wrong profile number files another company's
    documents under this one.
    """
    ids = (payload or {}).get("ids") or []
    if not ids:
        ids = [
            c.id for c in db.scalars(
                select(Company).where(
                    or_(Company.number.is_(None), Company.number == "")
                )
            )
        ]
    if not ids:
        raise HTTPException(409, "no companies are missing a SEDAR number")
    existing = db.scalar(
        select(Job).where(Job.kind == KIND_RESOLVE,
                          Job.status.in_([JOB_QUEUED, JOB_RUNNING]))
    )
    if existing:
        return _job_out(existing)
    job = Job(kind=KIND_RESOLVE, status=JOB_QUEUED,
              params=_json.dumps({"company_ids": ids}))
    db.add(job)
    db.commit()
    db.refresh(job)
    return _job_out(job)


@router.post("/catalog/publish")
def publish_catalog_to_d1(db: Session = Depends(get_db)):
    """Push the catalog to the Cloudflare D1 mirror.

    One-way and on demand (the worker also does this after an enumerate). A
    failure here means a stale mirror, never a broken scraper -- nothing in the
    download path reads D1.
    """
    from . import d1

    if not d1.enabled():
        raise HTTPException(
            503, "D1 is not configured (set D1_DATABASE_ID and D1_API_TOKEN)"
        )
    companies = list(db.scalars(select(Company).order_by(Company.name.asc())))
    try:
        return d1.publish_catalog(companies)
    except Exception as exc:
        raise HTTPException(502, f"D1 publish failed: {exc}")


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
    # Resuming picks up from a manually-paused enumerate's checkpoint page (the
    # worker fast-forwards to it); retire the paused record to keep the queue tidy.
    resume_page = 0
    for p in db.scalars(select(Job).where(Job.kind == KIND_ENUMERATE, Job.status == JOB_PAUSED)):
        resume_page = max(resume_page, p.batches_done or 0)
        p.status = JOB_DONE
        p.message = "resumed"
    job = q.enqueue_enumerate(
        db, profile_type=req.profile_type, max_pages=req.max_pages, start_page=resume_page
    )
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
        return _job_out(
            _require_job(
                q.enqueue_download(db, company, max_batches=req.max_batches), company
            )
        )
    return CompanyOut.model_validate(company)


@router.post("/companies/bulk")
def add_companies_bulk(req: BulkAddRequest, db: Session = Depends(get_db)):
    """Register many companies in one call. Upserts on SEDAR number.

    Downloads are NOT queued by default: adding a large watchlist and starting a
    thousand browser jobs are separate decisions, and one worker drives one
    browser serially. Pass download=true to queue as well. Paused companies are
    reported as skipped rather than silently woken.
    """
    added, updated, queued, skipped = 0, 0, 0, []
    for item in req.companies:
        number = (item.number or "").strip()
        if not number:
            skipped.append({"number": item.number, "reason": "missing SEDAR number"})
            continue
        company = db.scalar(select(Company).where(Company.number == number))
        if company is None:
            company = Company(
                number=number, name=(item.name or number).strip(), type="(added)"
            )
            db.add(company)
            added += 1
        else:
            if item.name:
                company.name = item.name.strip()
            updated += 1
        if item.exchange is not None:
            company.exchange = item.exchange.strip() or None
        if item.ticker is not None:
            company.ticker = item.ticker.strip() or None
        company.saved = True
        db.flush()
        if req.download:
            job = q.enqueue_download(db, company, max_batches=item.max_batches)
            if job is None:
                skipped.append({"number": number, "reason": "company is paused"})
            else:
                queued += 1
    db.commit()
    return {"added": added, "updated": updated, "queued": queued,
            "skipped": skipped, "total": len(req.companies)}


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
        job = _require_job(
            q.enqueue_download(db, company, max_batches=req.max_batches), company
        )
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
    job = _require_job(q.enqueue_download(db, company, max_batches=max_batches), company)
    return _job_out(job)


@router.post("/companies/{company_id}/recheck", response_model=JobOut)
def recheck_company(company_id: int, db: Session = Depends(get_db)):
    company = _get_company(db, company_id)
    job = _require_job(q.enqueue_recheck(db, company), company)
    return _job_out(job)


@router.post("/companies/{company_id}/pause", response_model=CompanyOut)
def pause_company(company_id: int, db: Session = Depends(get_db)):
    """Hard-stop all work for a company until it is explicitly unpaused.

    Stored on the company, so it outlives queue clears, worker restarts,
    automatic retries and cron rechecks. Anything already queued or running for
    it is cancelled -- a running job notices on its next recovery check and
    aborts.
    """
    company = _get_company(db, company_id)
    company.paused = True
    stopped = 0
    for j in db.scalars(
        select(Job).where(
            Job.company_id == company_id, Job.status.in_([JOB_QUEUED, JOB_RUNNING])
        )
    ):
        j.status = JOB_CANCELLED
        j.message = "cancelled — company paused"
        stopped += 1
    db.commit()
    print(f"[api] paused {company.name} (cancelled {stopped} job(s))", flush=True)
    return CompanyOut.model_validate(company)


@router.post("/companies/{company_id}/unpause", response_model=CompanyOut)
def unpause_company(company_id: int, db: Session = Depends(get_db)):
    """Lift the hard stop. Does not queue anything by itself."""
    company = _get_company(db, company_id)
    company.paused = False
    db.commit()
    return CompanyOut.model_validate(company)


@router.get("/companies/{company_id}/coverage")
def company_coverage(company_id: int, db: Session = Depends(get_db)):
    """Prove what is actually held for a company.

    ``indexed`` counts document rows; ``verified`` counts only those tied to a
    real file inside a downloaded archive. ``missing`` is measured against the
    total SEDAR+ reports, so a run that stopped early can't look finished.
    """
    company = _get_company(db, company_id)
    indexed = db.scalar(
        select(func.count(Document.id)).where(Document.company_id == company_id)
    ) or 0
    verified = db.scalar(
        select(func.count(Document.id)).where(
            Document.company_id == company_id, Document.content_sha256.is_not(None)
        )
    ) or 0
    batches = list(
        db.scalars(
            select(Batch).where(Batch.company_id == company_id).order_by(Batch.page.asc())
        )
    )
    archived_files = sum(b.member_count or 0 for b in batches)
    reported = company.reported_total or 0
    return {
        "company": company.name,
        "reported_total": reported,
        "indexed": indexed,
        "verified": verified,
        "archived_files": archived_files,
        "missing": max(0, reported - indexed),
        "complete": bool(company.is_complete),
        "short_batches": sum(1 for b in batches if b.status == BATCH_SHORT),
        "coverage_checked_at": company.coverage_checked_at,
        "batches": [
            {
                "page": b.page,
                "status": b.status,
                "expected": b.expected_count,
                "members": b.member_count,
                "zip_bytes": b.zip_bytes,
                "location": b.location,
                "note": b.note,
            }
            for b in batches
        ],
    }


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


@router.post("/queue/clear")
def clear_finished(db: Session = Depends(get_db)):
    """Delete finished jobs (done/failed/cancelled) from the queue history so the
    view stays tidy. Queued, running, and paused jobs are kept."""
    result = db.execute(
        delete(Job).where(Job.status.in_([JOB_DONE, JOB_FAILED, JOB_CANCELLED]))
    )
    db.commit()
    return {"cleared": result.rowcount}


@router.post("/queue/retry-failed")
def retry_failed(db: Session = Depends(get_db)):
    """Re-queue every failed download/recheck job (e.g. after a Radware
    throttling wave). The original failed row is retired to keep the queue tidy;
    the resume picks up where it left off (fast-forward, dedup-safe)."""
    import json as _json

    failed = list(
        db.scalars(
            select(Job).where(
                Job.status == JOB_FAILED, Job.kind.in_([KIND_DOWNLOAD, KIND_RECHECK])
            )
        )
    )
    requeued = 0
    for j in failed:
        company = db.get(Company, j.company_id) if j.company_id else None
        if company is None:
            continue
        max_batches = _json.loads(j.params or "{}").get("max_batches")
        if j.kind == KIND_RECHECK:
            again = q.enqueue_recheck(db, company, max_batches=max_batches)
        else:
            again = q.enqueue_download(db, company, max_batches=max_batches)
        if again is None:
            continue  # paused company: leave it alone, a bulk retry must not wake it
        j.status = JOB_CANCELLED  # retire the old failed row
        j.message = "retried"
        requeued += 1
    db.commit()
    return {"requeued": requeued}


@router.post("/companies/{company_id}/reset")
def reset_company_documents(
    company_id: int,
    x_admin_token: str | None = Header(default=None),
    db: Session = Depends(get_db),
):
    """Clear a company's indexed documents so the next download re-pulls every
    page from scratch. Use after a bad run indexed rows against a wrong/partial
    zip. Does not touch R2 objects. Admin-gated."""
    if not settings.admin_token:
        raise HTTPException(403, "reset is disabled (set ADMIN_TOKEN to enable)")
    if x_admin_token != settings.admin_token:
        raise HTTPException(401, "invalid admin token")
    company = _get_company(db, company_id)
    n = db.execute(delete(Document).where(Document.company_id == company_id)).rowcount
    db.execute(delete(Batch).where(Batch.company_id == company_id))
    company.total_documents = 0
    company.is_complete = False
    company.last_download_at = None
    db.commit()
    return {"company": company.name, "documents_deleted": n}


@router.post("/jobs/{job_id}/force-fail", response_model=JobOut)
def force_fail_job(
    job_id: int,
    x_admin_token: str | None = Header(default=None),
    db: Session = Depends(get_db),
):
    """Force a job to 'failed' regardless of its current state — the escape hatch
    for a job stuck in a crash/restart loop that normal cancel (queued-only)
    can't reach. Setting it 'failed' means requeue_stuck won't revive it on the
    next worker restart, so the loop breaks and the worker goes idle. Admin-gated.
    """
    if not settings.admin_token:
        raise HTTPException(403, "force-fail is disabled (set ADMIN_TOKEN to enable)")
    if x_admin_token != settings.admin_token:
        raise HTTPException(401, "invalid admin token")
    from datetime import datetime, timezone

    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    job.status = JOB_FAILED
    job.blocked = False
    job.pause_requested = False
    job.message = "force-failed by admin (was looping)"
    job.finished_at = datetime.now(timezone.utc)
    db.commit()
    return _job_out(job)


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
    # enqueue_recheck returns None for a paused company; the daily cron must not
    # be the thing that quietly resurrects one.
    jobs = [j for j in (q.enqueue_recheck(db, c) for c in companies) if j is not None]
    skipped = len(companies) - len(jobs)
    return {"queued": len(jobs), "skipped_paused": skipped,
            "job_ids": [j.id for j in jobs]}


# --------------------------------------------------------------------------- #
# Admin (destructive; gated behind ADMIN_TOKEN)
# --------------------------------------------------------------------------- #
def _dir_size(path) -> tuple[int, int]:
    """(bytes, file_count) for a directory tree; missing paths report zero."""
    total = files = 0
    try:
        for p in path.rglob("*"):
            if p.is_file():
                try:
                    total += p.stat().st_size
                    files += 1
                except OSError:
                    pass
    except (OSError, ValueError):
        pass
    return total, files


@router.get("/admin/storage")
def admin_storage(x_admin_token: str | None = Header(default=None)):
    """What is actually occupying the persistent volume.

    With R2 configured the volume only needs to hold sedar.db -- archives are
    staged on ephemeral disk and deleted after upload. Anything else under
    downloads/ is left over from local-mode runs and is reported here so it can
    be reviewed before removal (those files predate R2 and may be the only copy,
    so nothing is deleted automatically)."""
    if not settings.admin_token:
        raise HTTPException(403, "admin storage view is disabled (set ADMIN_TOKEN)")
    if x_admin_token != settings.admin_token:
        raise HTTPException(401, "invalid admin token")
    import shutil as _shutil

    data_dir = settings.data_dir
    usage = _shutil.disk_usage(data_dir)
    db_path = data_dir / "sedar.db"
    entries = []
    try:
        for child in sorted(data_dir.iterdir()):
            size, files = _dir_size(child) if child.is_dir() else (
                child.stat().st_size, 1
            )
            entries.append(
                {
                    "name": child.name,
                    "kind": "dir" if child.is_dir() else "file",
                    "bytes": size,
                    "mb": round(size / (1024 * 1024), 1),
                    "files": files,
                }
            )
    except OSError as e:
        raise HTTPException(500, f"cannot read {data_dir}: {e}")

    leftovers = []
    if settings.r2_enabled and settings.download_dir.exists():
        for child in sorted(settings.download_dir.iterdir()):
            size, files = _dir_size(child) if child.is_dir() else (
                child.stat().st_size, 1
            )
            leftovers.append(
                {"name": child.name, "mb": round(size / (1024 * 1024), 1), "files": files}
            )

    return {
        "data_dir": str(data_dir),
        "volume_total_mb": round(usage.total / (1024 * 1024), 1),
        "volume_used_mb": round((usage.total - usage.free) / (1024 * 1024), 1),
        "volume_free_mb": round(usage.free / (1024 * 1024), 1),
        "database_mb": round(db_path.stat().st_size / (1024 * 1024), 1)
        if db_path.exists() else 0,
        "staging_dir": str(settings.staging_dir),
        "staging_on_volume": settings.staging_dir == settings.download_dir,
        "entries": entries,
        "local_archive_leftovers": leftovers,
    }


@router.post("/admin/storage/purge-local-archives")
def admin_purge_local_archives(
    confirm: bool = Query(False, description="must be true to actually delete"),
    x_admin_token: str | None = Header(default=None),
):
    """Delete leftover local archive folders under downloads/ (R2 mode only).

    Defaults to a dry run. These files are NOT mirrored to R2 automatically --
    early runs happened before R2 was configured -- so review the listing from
    /admin/storage first; this only ever touches the local volume, never R2.
    """
    if not settings.admin_token:
        raise HTTPException(403, "admin purge is disabled (set ADMIN_TOKEN)")
    if x_admin_token != settings.admin_token:
        raise HTTPException(401, "invalid admin token")
    if not settings.r2_enabled:
        raise HTTPException(
            409, "refusing to purge: R2 is not configured, so these are the only copies"
        )
    import shutil as _shutil

    removed, freed = [], 0
    for child in sorted(settings.download_dir.iterdir()):
        size, _files = _dir_size(child) if child.is_dir() else (child.stat().st_size, 1)
        removed.append({"name": child.name, "mb": round(size / (1024 * 1024), 1)})
        freed += size
        if confirm:
            try:
                _shutil.rmtree(child) if child.is_dir() else child.unlink()
            except OSError as e:
                raise HTTPException(500, f"failed removing {child.name}: {e}")
    return {
        "dry_run": not confirm,
        "would_free_mb" if not confirm else "freed_mb": round(freed / (1024 * 1024), 1),
        "items": removed,
    }


@router.post("/admin/purge-companies")
def purge_companies(
    payload: dict | None = None,
    x_admin_token: str | None = Header(default=None),
    db: Session = Depends(get_db),
):
    """Delete catalog companies that hold no documents, keeping the ones that do.

    For clearing an enumerate-derived catalog before importing a curated list.
    Keeps any company with total_documents > 0; everything else goes, along with
    its documents, batches and queue history (children first, for the foreign
    keys). Dry run by default -- pass {"confirm": true} to actually delete.

    Never touches R2. The archives for a purged company stay exactly where they
    are; only this database's record of them is removed.
    """
    if not settings.admin_token:
        raise HTTPException(403, "purge is disabled (set ADMIN_TOKEN to enable)")
    if x_admin_token != settings.admin_token:
        raise HTTPException(401, "invalid admin token")

    keep_ids = [
        c.id for c in db.scalars(select(Company).where(Company.total_documents > 0))
    ]
    if not keep_ids:
        raise HTTPException(
            409, "refusing to purge: no company has documents, which would delete "
                 "the entire catalog -- use /admin/reset if that is the intent"
        )
    doomed = list(db.scalars(select(Company).where(Company.id.notin_(keep_ids))))
    doomed_ids = [c.id for c in doomed]

    if not (payload or {}).get("confirm"):
        return {
            "dry_run": True,
            "would_delete": len(doomed_ids),
            "would_keep": len(keep_ids),
            "keeping": sorted(
                (c.folder_slug or c.name) for c in
                db.scalars(select(Company).where(Company.id.in_(keep_ids)))
            ),
            "note": "pass {\"confirm\": true} to apply; R2 is never touched",
        }

    n_docs = db.execute(delete(Document).where(Document.company_id.in_(doomed_ids))).rowcount
    n_batches = db.execute(delete(Batch).where(Batch.company_id.in_(doomed_ids))).rowcount
    n_jobs = db.execute(delete(Job).where(Job.company_id.in_(doomed_ids))).rowcount
    n_companies = db.execute(delete(Company).where(Company.id.in_(doomed_ids))).rowcount
    db.commit()

    kept = list(db.scalars(select(Company)))
    result = {
        "purged": True,
        "deleted": {"companies": n_companies, "documents": n_docs,
                    "batches": n_batches, "jobs": n_jobs},
        "remaining": len(kept),
        "note": "R2 objects were not touched",
    }
    # Bring the mirror in line in the same breath, so the two never disagree.
    try:
        from . import d1

        if d1.enabled():
            result["d1"] = d1.prune_catalog([c.number for c in kept])
    except Exception as exc:
        result["d1_error"] = f"{exc}"
    return result


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

