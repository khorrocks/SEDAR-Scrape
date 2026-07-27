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

import json as _json
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
from .archive import inspect_zip, match_members
from .config import settings
from .models import BATCH_OK, BATCH_SHORT, Batch, Company, Document

ProgressFn = Callable[[int, int, int, str], None]
"""(batches_done, documents_done, total_documents, message) -> None"""


class IncompleteDownload(Exception):
    """A full download ended holding fewer documents than SEDAR+ reports.

    Raised so the job does NOT finish as a success: the worker's recovery loop
    rebuilds the browser and resumes from the checkpoint page, and if repeated
    attempts add nothing the job ends FAILED and visible. Previously such a run
    returned normally, so a company that stopped 126 documents short was marked
    'done' and the queue moved on to the next company.
    """


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


def _page_dedup_keys(rows: list[dict]) -> list[str]:
    """Dedup keys for one page, disambiguating genuine duplicates.

    Two distinct filings can share title + submitted timestamp + size (seen live:
    a 30-row page yielded only 29 keys, so one filing could never be indexed and
    the company was permanently stuck one short). Repeat occurrences get a "|#n"
    suffix, keyed off their order within the page -- stable because the result
    set is server-sorted. The FIRST occurrence keeps the original key, so every
    previously stored key still matches and nothing is re-downloaded.
    """
    seen: dict[str, int] = {}
    keys: list[str] = []
    for row in rows:
        base = _dedup_key(row)
        n = seen.get(base, 0) + 1
        seen[base] = n
        keys.append(base if n == 1 else f"{base}|#{n}")
    return keys


def _total_from_count_line(line: str) -> int:
    m = re.search(r"of\s+([\d,]+)\s+results", line or "")
    return int(m.group(1).replace(",", "")) if m else 0


_COUNT_RE = re.compile(
    r"Displaying\s+([\d,]+)\s*[-–]\s*([\d,]+)\s+of\s+([\d,]+)\s+results", re.I
)


def _parse_count_line(line: str) -> tuple[int, int, int]:
    """(first, last, total) from 'Displaying 31-60 of 636 results', else zeros.

    ``last`` vs ``total`` is the authoritative end-of-results signal: a missing
    "Next" control cannot distinguish the real last page from broken pagination,
    so a run could stop early and still report success.
    """
    m = _COUNT_RE.search(line or "")
    if not m:
        return 0, 0, _total_from_count_line(line)
    return tuple(int(g.replace(",", "")) for g in m.groups())  # type: ignore[return-value]


# How many times to re-download a page whose archive came back short/corrupt
# before accepting it and flagging the batch.
_SHORT_RETRIES = 2


def _advance_page(driver, prev_first: int, tries: int = 15, reclicks: int = 2) -> bool:
    """Click 'Next' and wait until the results table has REALLY changed.

    ``profiles.next_page`` only clicks and sleeps a fixed interval. If the table
    has not re-rendered by then, the next scrape returns the SAME rows -- every
    key is already known, so the page yields "0 new", no archive is downloaded,
    and a whole page of filings is silently skipped. Seen live: an otherwise
    contiguous run went ...batch0004, batch0006..., losing 30 documents. Polling
    the "Displaying X-Y of N" line until X moves makes the advance verifiable.
    """
    # Re-click as well as re-poll: right after a browser rebuild the table can be
    # slow enough that a single click plus a short wait isn't enough, and giving
    # up here ends the run early.
    for attempt in range(reclicks + 1):
        if not profiles.next_page(driver):
            if attempt == 0:
                return False  # genuinely no Next control
            break
        for _ in range(tries):
            first, _last, _total = _parse_count_line(docs.result_count(driver))
            if first and first != prev_first:
                return True
            time.sleep(2)
        print(
            f"[scraper] still on row {prev_first} after Next "
            f"(attempt {attempt + 1}/{reclicks + 1})",
            flush=True,
        )
    print(
        f"[scraper] page did not advance past row {prev_first} after clicking Next",
        flush=True,
    )
    return False


def _download_verified(
    driver, expected: int, row_indices: list[int] | None = None
) -> tuple[Path | None, dict, bool]:
    """Download the current page's archive and check it really holds the files.

    Returns ``(path, manifest, short)``. A short or corrupt archive is discarded
    and retried; if it is still short on the last attempt we keep it (so whatever
    did arrive is preserved) and report ``short=True`` so the caller can flag the
    batch and index only the documents actually present.
    """
    path: Path | None = None
    info: dict = {}
    for attempt in range(_SHORT_RETRIES + 1):
        fname = docs.download_current_page(
            driver, settings.staging_dir, timeout=settings.download_timeout_seconds,
            row_indices=row_indices,
        )
        path = settings.staging_dir / fname
        info = inspect_zip(path)
        if info["ok"] and info["member_count"] >= expected:
            return path, info, False
        note = info.get("error") or f"{info['member_count']} of {expected} file(s)"
        print(
            f"[scraper] batch archive incomplete ({note}); "
            f"attempt {attempt + 1}/{_SHORT_RETRIES + 1}",
            flush=True,
        )
        if attempt < _SHORT_RETRIES:
            try:
                path.unlink()
            except OSError:
                pass
            path = None
            time.sleep(settings.batch_pause_seconds)
    return path, info, True


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

    short_batches = 0
    premature_stop = False
    while True:
        page += 1
        rows = docs.list_page_rows(driver)
        page_keys = _page_dedup_keys(rows)
        new_pairs = [(r, k) for r, k in zip(rows, page_keys) if k not in known]
        page_new = [r for r, _ in new_pairs]

        # Where this page sits in the whole result set; drives the end-of-results
        # check below and records the yardstick for completeness.
        first_idx, last_idx, reported = _parse_count_line(docs.result_count(driver))
        if reported:
            total = reported
        elif not reported:
            # The count line can be unreadable for a page (notably right after a
            # browser rebuild). Falling back to the total already known for this
            # company matters: every end-of-results and premature-stop check is
            # conditioned on a truthy `reported`, so a zero here silently
            # disabled BOTH and let a short run finish as a success.
            reported = total or (company.reported_total or 0)

        if only_new and not page_new and page > start_page:
            # Newest-first: a page with nothing new means we've caught up.
            # Only past the resume point though -- after a browser rebuild the
            # run fast-forwards back onto a page it already downloaded, which
            # legitimately has nothing new. Treating that as "caught up" ended
            # every recheck at its first rebuild (seen live: a recheck stopped at
            # 150 of 675 documents, exactly DOWNLOAD_REBUILD_EVERY x 30).
            break

        if page_new:
            ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            # Only fetch the documents we don't already hold. When the entire
            # page is new (a first full download) fall back to the page-level
            # "all documents" checkbox, which is the verified path; when only a
            # few are new (a recheck) tick just those rows.
            row_indices = None
            if len(page_new) < len(rows):
                row_indices = [
                    r["row_index"] for r in page_new if r.get("row_index") is not None
                ]
                print(
                    f"[scraper] page {page}: fetching {len(row_indices)} new of "
                    f"{len(rows)} document(s)",
                    flush=True,
                )
            src, info, short = _download_verified(driver, len(page_new), row_indices)
            zip_rel = None
            if src is not None and src.exists():
                dest_name = f"{ts}_batch{page:04d}.zip"
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

            members = info.get("members", [])
            batch = Batch(
                company_id=company.id,
                page=page,
                location=zip_rel,
                expected_count=len(page_new),
                member_count=info.get("member_count", 0),
                zip_bytes=info.get("zip_bytes", 0),
                total_bytes=info.get("total_bytes", 0),
                manifest=_json.dumps(members),
                status=BATCH_SHORT if short else BATCH_OK,
                note=info.get("error"),
            )
            db.add(batch)
            db.flush()  # need batch.id for the document rows
            if short:
                short_batches += 1
                print(
                    f"[scraper] page {page} flagged short: archive holds "
                    f"{info.get('member_count', 0)} of {len(page_new)} expected file(s)",
                    flush=True,
                )

            # Pair each new row with the file that actually arrived. When the
            # archive came up short we index ONLY the matched rows, so the
            # database never claims a document we do not hold -- the rest stay
            # unknown and are picked up by a later pass.
            matches = match_members([r.get("document", "") for r in page_new], members)
            for (r, k), member in zip(new_pairs, matches):
                if short and member is None:
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
                        batch_id=batch.id,
                        archive_member=member["name"] if member else None,
                        content_sha256=member["sha256"] if member else None,
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
        # Authoritative end of results: the last row on this page is the last of
        # N. Checking this before "is there a Next button" is what distinguishes
        # finishing from pagination silently breaking part-way through.
        if reported and last_idx and last_idx >= reported:
            break
        time.sleep(settings.batch_pause_seconds)
        if not _advance_page(driver, first_idx):
            if reported and last_idx and last_idx < reported:
                premature_stop = True
                print(
                    f"[scraper] pagination stopped at {last_idx} of {reported} "
                    "results -- company is incomplete",
                    flush=True,
                )
            break

    now = datetime.now(timezone.utc)
    company.total_documents = len(known)
    if total:
        company.reported_total = total
    # Only claim completeness when the indexed count reaches what the site
    # reports and nothing was flagged along the way.
    company.is_complete = bool(
        total and len(known) >= total and not short_batches and not premature_stop
    )
    company.coverage_checked_at = now
    company.last_checked_at = now
    if new_docs:
        company.last_download_at = now
    db.commit()

    result = {
        "batches": batches,
        "new_documents": new_docs,
        "total_reported": total or company.reported_total or 0,
        "indexed": len(known),
        "short_batches": short_batches,
        "premature_stop": premature_stop,
        "complete": company.is_complete,
    }

    # A full download that ended short must not be reported as success. Recheck
    # mode stops deliberately once it catches up, and a capped test run stops by
    # design, so neither counts as incomplete.
    target = total or company.reported_total or 0
    if not only_new and not max_batches and target and len(known) < target:
        raise IncompleteDownload(
            f"{company.name}: holding {len(known)} of {target} documents "
            f"({target - len(known)} missing"
            + (", pagination stopped early" if premature_stop else "")
            + (f", {short_batches} short batch(es)" if short_batches else "")
            + ")"
        )
    return result


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
