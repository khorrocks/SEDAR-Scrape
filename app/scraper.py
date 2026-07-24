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

from . import r2
from .config import settings
from .models import Company, Document

ProgressFn = Callable[[int, int, int, str], None]
"""(batches_done, documents_done, total_documents, message) -> None"""


class ProactiveRebuild(Exception):
    """Raised by a long download to ask the worker to rebuild the browser (free
    memory) and resume from the current page -- a planned memory reset, not a
    failure. The worker's recovery loop handles it without counting a retry."""


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
    start_page: int = 0,
) -> dict:
    """Download a company's documents in batches of 30 and index them.

    ``only_new`` (recheck mode) stops once a page yields no new documents, which
    works because SEDAR+ lists newest filings first. Returns a summary dict.

    ``start_page`` resumes an interrupted download: the worker persists the
    current results page (in ``batches_done``), so on a restart we *fast-forward*
    to that page instead of re-walking from page 1. Already-downloaded batches are
    still skipped via the dedup ``known`` set, so nothing is re-fetched.
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

    # R2 is the source of truth when configured; batch zips are uploaded to
    # <prefix>/<slug>/raw-data/ and the local copy deleted. Falls back to local
    # disk when no R2 credentials are set.
    use_r2 = settings.r2_enabled
    slug = company.folder_slug
    if use_r2 and not slug:
        raise RuntimeError(
            f"set an exchange and ticker for {company.name} (#{company.number}) "
            "before downloading — together they name its R2 folder"
        )

    # Mirror the R2 layout on local disk: <slug>/raw-data/. Prefer the slug and
    # fall back to the issuer number only when exchange/ticker aren't set.
    company_dir = (
        settings.download_dir
        / (slug or company.number or f"company_{company.id}")
        / "raw-data"
    )
    if not use_r2:
        company_dir.mkdir(parents=True, exist_ok=True)

    batches = 0
    new_docs = 0
    page = 0

    # Resume: fast-forward (no scraping/download) to the checkpoint results page
    # so a restarted download continues where it stopped rather than re-walking
    # from page 1. resolve_profile leaves us on page 1; safe by construction --
    # an advance that lags lands earlier and re-scrapes (dedup skips known).
    if start_page and start_page > 1:
        page = 1
        while page < start_page:
            if progress:
                # Keep batches_done pinned at the target so a crash during
                # fast-forward still resumes there.
                progress(start_page, new_docs, total, f"resuming: fast-forwarding to page {page}/{start_page}")
            if not _advance_or_raise_if_blocked(driver, _FF_SETTLE):
                break  # genuine end of results before the resume point
            page += 1
        page -= 1  # the loop's page += 1 below lands us back on this page

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
            fname = docs.download_current_page(
                driver, settings.download_dir, timeout=settings.download_timeout_seconds
            )
            if fname:
                src = settings.download_dir / fname
                dest_name = f"{ts}_batch{page:04d}.zip"
                if src.exists():
                    if use_r2:
                        # Upload to R2, then delete the local copy so the volume
                        # stays small. batch_zip stores the R2 object key.
                        key = r2.raw_data_key(slug, dest_name)
                        r2.upload_file(src, key)
                        src.unlink()
                        zip_rel = key
                    else:
                        # Local fallback: move out of the shared dir into the
                        # company's folder; batch_zip stores a data_dir-relative path.
                        dest = company_dir / dest_name
                        shutil.move(str(src), str(dest))
                        zip_rel = str(dest.relative_to(settings.data_dir))

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
            # batches_done carries the results page so a restart resumes here.
            progress(page, new_docs, total, f"page {page} ({len(page_new)} new)")

        if max_batches and batches >= max_batches:
            break
        # Planned memory reset: after N downloaded batches, rebuild the browser
        # (via the recovery loop) and resume from this page (fast-forward). The
        # batch just downloaded is committed, so nothing is lost.
        if (settings.download_rebuild_every_batches
                and batches >= settings.download_rebuild_every_batches):
            raise ProactiveRebuild()
        time.sleep(settings.batch_pause_seconds)
        if not _advance_or_raise_if_blocked(driver, settle=8.0):
            break

    now = datetime.now(timezone.utc)
    company.total_documents = len(known)
    company.last_checked_at = now
    if new_docs:
        company.last_download_at = now
    db.commit()

    return {"batches": batches, "new_documents": new_docs, "total_reported": total}


_FF_SETTLE = 3.0  # pause between pages while fast-forwarding (no scraping)


def _advance_or_raise_if_blocked(driver, settle: float) -> bool:
    """Click 'Next'. Return True if we advanced, False at the genuine end of the
    list. If we couldn't advance because of a Radware/captcha page (not the end),
    raise so the worker's recovery pauses for a manual solve instead of silently
    treating a mid-walk block as 'done'."""
    if profiles.next_page(driver, settle=settle):
        return True
    if docs.is_blocked(driver):
        raise RuntimeError("Radware block during enumeration — solve the captcha to continue")
    return False


def enumerate_catalog(db: Session, driver, profile_type: str | None = "Company",
                      max_pages: int | None = None, progress: ProgressFn | None = None,
                      should_yield: Callable[[], bool] | None = None,
                      start_page: int = 0) -> dict:
    """Populate/refresh the companies catalog used by autocomplete.

    Pages through the Reporting issuers list and upserts each page immediately
    (checkpointing), so a long run survives an interruption. Upserts are
    idempotent (keyed on issuer number), so re-runs accumulate the full list.

    ``start_page`` resumes a paused/interrupted run: the list only has a 'Next'
    button (no page jump), so we *fast-forward* to that page by paging without
    scraping, then resume scraping. Safe by construction -- if an advance lags we
    land earlier and re-scrape (idempotent), never past unscraped pages.

    ``should_yield`` is polled after each page; if it returns True (a waiting
    download, or a manual pause) the enumerate stops early and returns
    ``{"yielded": True}``. Returns ``{"seen": <int>, "yielded": <bool>}``.
    """
    profiles.open_reporting_issuers(driver)
    col = profiles._column_index(driver)
    total = profiles.total_count(driver)

    seen: set[str] = set()
    page = 1  # open_reporting_issuers leaves us on page 1

    # Resume: fast-forward (no scraping) to the checkpoint page.
    if start_page and start_page > page:
        while page < start_page:
            if should_yield and should_yield():
                return {"seen": len(seen), "yielded": True}
            if not _advance_or_raise_if_blocked(driver, _FF_SETTLE):
                break  # genuine end of list before the resume point
            page += 1
            if progress:
                # Keep batches_done pinned at the resume target so a crash during
                # fast-forward still resumes there (not the fast-forward position).
                progress(start_page, 0, total or 0, f"resuming: fast-forwarding {page}/{start_page}")

    while True:
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
        # Pause point: step aside for a waiting download/recheck or a manual
        # pause. Checkpoint above means nothing is lost.
        if should_yield and should_yield():
            return {"seen": len(seen), "yielded": True}
        if not _advance_or_raise_if_blocked(driver, 4.0):
            break
        page += 1
    return {"seen": len(seen), "yielded": False}
