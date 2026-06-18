"""Orchestrates a full company download against SEDAR+ and records results.

This is the only place that joins the (verified) ``sedar`` browser steps to the
database. The worker calls :func:`download_company`; everything here runs inside
the single worker process that owns the one browser.

Batches of 30
-------------
SEDAR+ paginates document results 30 per page, and the bulk download works
per-page (tick "all documents listed on this page" -> zip). So one results page
== one batch of (up to) 30 documents == one zip. We page through, downloading a
zip per page, until there are no more pages (full download) or until we reach
only already-known documents (recheck).
"""

from __future__ import annotations

import re
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from sedar import documents as docs
from sedar import lookup, profiles
from sedar.browser import BrowserConfig, build_driver

from .config import settings
from .models import Company, Document

ProgressFn = Callable[[int, int, int, str], None]
"""(batches_done, documents_done, total_documents, message) -> None"""


def make_driver(download_dir: Path):
    cfg = BrowserConfig(
        download_dir=download_dir,
        chrome_binary=settings.chrome_binary,
        chromedriver_binary=settings.chromedriver_binary,
        version_main=settings.chrome_version,
        headless=settings.headless,
        ignore_cert_errors=settings.ignore_cert_errors,
    )
    return build_driver(cfg)


def _dedup_key(row: dict) -> str:
    parts = [row.get("document", ""), row.get("submitted", ""), row.get("file_size", "")]
    return "|".join(p.strip() for p in parts)


def _total_from_count_line(line: str) -> int:
    m = re.search(r"of\s+([\d,]+)\s+results", line or "")
    return int(m.group(1).replace(",", "")) if m else 0


def resolve_profile(driver, company: Company) -> bool:
    """Ensure ``company`` can be opened for document download.

    Prefer the verified ``profile.html?id=`` path; if we have never resolved the
    company, try to capture the id via "Generate URL" and persist it. Returns
    True if the driver is left on a searchable document results page.
    """
    if company.profile_id:
        docs.open_profile_documents(driver, company.profile_id)
        docs.run_search(driver)
        return True

    # Enumerated companies only have a Number: drive the documents search from
    # it (bootstrap session -> searchDocuments -> 'Profile name or number').
    if lookup.open_documents_by_number(driver, company.number):
        return True
    return False


def download_company(
    db: Session,
    driver,
    company: Company,
    *,
    only_new: bool = False,
    max_batches: int | None = None,
    progress: ProgressFn | None = None,
) -> dict:
    """Download a company's documents in batches of 30 and index them.

    ``only_new`` (recheck mode) stops once a page yields no new documents, which
    works because SEDAR+ lists newest filings first. Returns a summary dict.
    """
    if not resolve_profile(driver, company):
        raise RuntimeError(
            f"could not resolve a document search for {company.name} ({company.number})"
        )

    total = _total_from_count_line(docs.result_count(driver))
    known = {
        d.dedup_key for d in db.scalars(
            select(Document).where(Document.company_id == company.id)
        )
    }

    company_dir = settings.download_dir / (company.number or f"company_{company.id}")
    company_dir.mkdir(parents=True, exist_ok=True)

    batches = 0
    new_docs = 0
    page = 0
    while True:
        page += 1
        rows = docs.list_page_rows(driver)
        page_keys = [_dedup_key(r) for r in rows]
        page_new = [r for r, k in zip(rows, page_keys) if k not in known]

        if only_new and not page_new:
            # Newest-first: a page with nothing new means we've caught up.
            break

        zip_rel = None
        if page_new:
            ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            before = {p.name for p in settings.download_dir.iterdir()}
            fname = docs.download_current_page(
                driver, settings.download_dir, timeout=settings.download_timeout_seconds
            )
            if fname:
                src = settings.download_dir / fname
                dest = company_dir / f"{ts}_batch{page:04d}.zip"
                # Move the just-downloaded zip out of the shared dir into the
                # company's folder so concurrent before/after sets stay clean.
                if src.exists():
                    shutil.move(str(src), str(dest))
                    zip_rel = str(dest.relative_to(settings.data_dir))
            else:
                _ = before  # download timed out; leave zip_rel None

            for r, k in zip(rows, page_keys):
                if k in known:
                    continue
                db.add(
                    Document(
                        company_id=company.id,
                        title=r.get("document", ""),
                        submitted=r.get("submitted"),
                        jurisdiction=r.get("jurisdiction"),
                        file_size=r.get("file_size"),
                        dedup_key=k,
                        batch_zip=zip_rel,
                    )
                )
                known.add(k)
                new_docs += 1
            batches += 1
            db.commit()

        if progress:
            progress(batches, new_docs, total, f"page {page} ({len(page_new)} new)")

        if max_batches and batches >= max_batches:
            break
        time.sleep(settings.batch_pause_seconds)
        if not profiles.next_page(driver):
            break

    now = datetime.now(timezone.utc)
    company.total_documents = len(known)
    company.last_checked_at = now
    if new_docs:
        company.last_download_at = now
    db.commit()

    return {"batches": batches, "new_documents": new_docs, "total_reported": total}


def enumerate_catalog(db: Session, driver, profile_type: str | None = "Company",
                      max_pages: int | None = None, progress: ProgressFn | None = None) -> int:
    """Populate/refresh the companies catalog used by autocomplete.

    Pages through the Reporting issuers list and upserts each page immediately
    (checkpointing), so a long run survives an interruption. Returns the number
    of catalog rows touched. Resuming re-walks from the top, but upserts are
    idempotent (keyed on issuer number), so re-runs accumulate the full list.
    """
    profiles.open_reporting_issuers(driver)
    col = profiles._column_index(driver)
    total = profiles.total_count(driver)

    seen: set[str] = set()
    page = 0
    while True:
        page += 1
        for r in profiles.scrape_page(driver, col):
            if profile_type and profile_type.lower() not in (r.get("type") or "").lower():
                continue
            number = (r.get("number") or "").strip()
            if not number or number in seen:
                continue
            seen.add(number)
            existing = db.scalar(select(Company).where(Company.number == number))
            if existing:
                existing.name = r.get("name") or existing.name
                existing.jurisdiction = r.get("jurisdiction") or existing.jurisdiction
                existing.type = r.get("type") or existing.type
            else:
                db.add(
                    Company(
                        number=number,
                        name=r.get("name", ""),
                        jurisdiction=r.get("jurisdiction"),
                        type=r.get("type"),
                    )
                )
        db.commit()  # checkpoint after every page
        if progress:
            progress(page, len(seen), total or 0, f"page {page}: {len(seen)} issuers")
        if max_pages and page >= max_pages:
            break
        if not profiles.next_page(driver, settle=4.0):  # light list: short settle
            break
    return len(seen)
