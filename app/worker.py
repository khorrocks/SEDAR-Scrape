"""The single background worker.

It owns the one Chrome instance and drains the job queue strictly serially:
claim oldest queued job -> run to completion -> repeat. Because there is exactly
one worker and one browser, a company's full multi-batch download always
finishes before the next company starts -- which is the queue behaviour the UI
visualises.

Run it as its own process (see start.sh / Procfile):
    python -m app.worker
On a headless server it must run under Xvfb so real (non-headless) Chrome works:
    xvfb-run -a -s "-screen 0 1920x1400x24" python -m app.worker
"""

from __future__ import annotations

import json
from json import dumps as _json_dumps
import os
import signal
import threading
import time
import traceback

from .config import settings
from .db import init_db, session_scope
from sqlalchemy import func, select

from sedar import documents as sedar_docs

from .models import (
    JOB_PAUSED,
    JOB_QUEUED,
    JOB_RUNNING,
    KIND_DOWNLOAD,
    KIND_ENUMERATE,
    KIND_PROBE,
    KIND_RECHECK,
    KIND_RESOLVE,
    Company,
    Document,
    Job,
)


def _doc_count(db, company_id: int) -> int:
    return db.scalar(
        select(func.count(Document.id)).where(Document.company_id == company_id)
    ) or 0


def _company_count(db) -> int:
    return db.scalar(select(func.count(Company.id))) or 0


def _publish_filings(db) -> None:
    """Push filing dates to the mirror after a download, best-effort.

    New documents mean new (company, filing_id) pairs, and the archives in R2
    carry PDFs only -- without this the downstream ingest gets files it cannot
    date. Never allowed to fail the download that just succeeded.
    """
    if not settings.d1_auto_publish:
        return
    try:
        from . import d1
        from .api import _filing_rows

        if not d1.enabled():
            return
        rows = _filing_rows(db)
        if rows:
            print(f"[worker] D1 filings: {d1.publish_filings(rows)}", flush=True)
    except Exception as exc:
        print(f"[worker] D1 filing publish failed: {exc}", flush=True)


def _publish_d1(db, job) -> None:
    """Refresh the D1 catalog mirror, best-effort.

    Called after the jobs that change what the mirror should say: an enumerate
    (new companies) and a resolve (new SEDAR numbers). The number matters
    especially -- it is the mirror's primary key, so a company only becomes
    publishable once it has one.

    Deliberately swallows every error: a publish failure means a stale mirror,
    and failing the scrape job that just succeeded over that would be backwards.
    """
    if not settings.d1_auto_publish:
        return
    try:
        from . import d1

        if not d1.enabled():
            return
        summary = d1.publish_catalog(list(db.scalars(select(Company))))
        print(f"[worker] D1 mirror updated: {summary}", flush=True)
        if job is not None:
            job.message = (job.message or "") + f"; D1 mirror updated ({summary['published']})"
    except Exception as exc:
        print(f"[worker] D1 publish failed (mirror is stale): {exc}", flush=True)


def _sleep_alive(seconds: float) -> None:
    """Sleep while telling the watchdog we are still healthy.

    A backoff is deliberate waiting, not a stall. Sleeping straight through it
    was fatal: the download timeout (240s) plus the backoff (180s) equals the
    watchdog threshold (420s) exactly, so the worker was killed at the precise
    moment it was about to retry -- the same cycle repeating forever without ever
    getting a second attempt.
    """
    deadline = time.time() + seconds
    while _RUNNING and time.time() < deadline:
        time.sleep(min(20.0, max(0.0, deadline - time.time())))
        _beat()


def _driver_in_maintenance(holder) -> bool:
    d = holder._driver
    if d is None:
        return False
    try:
        return sedar_docs.is_maintenance(d)
    except Exception:
        return False


def _driver_is_blocked(holder) -> bool:
    """True if the current browser is sitting on a Radware/captcha page."""
    d = holder._driver
    if d is None:
        return False
    try:
        return sedar_docs.is_blocked(d)
    except Exception:
        return False


def _await_manual_solve(db, job, holder) -> bool:
    """Pause on a captcha and wait for a human to solve it in the live browser
    view. Crucially we DON'T rebuild the browser (that would discard the session
    the human just cleared); we poll the same driver until it leaves the block
    page, then let the caller retry. Returns True if solved, False on timeout."""
    d = holder._driver
    if d is None:
        return False
    job.blocked = True
    job.message = ("CAPTCHA detected — open the live browser view and solve it "
                   "to continue")
    db.commit()
    # Nothing proceeds until a human solves this, so it is worth interrupting
    # someone for. Rate-limited inside notify.slack: the recovery loop re-detects
    # the same wall on every retry.
    notify.slack(
        "SEDAR Scraper: CAPTCHA wall hit.  Please solve CAPTCHA on Live Viewer",
        key="captcha-wall",
    )
    print(f"[worker] job {job.id} paused for manual CAPTCHA solve "
          f"(up to {int(settings.captcha_wait_seconds)}s)", flush=True)
    deadline = time.time() + settings.captcha_wait_seconds
    while _RUNNING and time.time() < deadline:
        time.sleep(3)
        # Waiting for a human is not a stall. Without this the watchdog fired at
        # 420s and restarted the worker mid-wait, so the solve window was never
        # the configured 600s -- it just re-detected the CAPTCHA and started over.
        _beat()
        try:
            still_blocked = sedar_docs.is_blocked(d)
        except Exception:
            break  # browser died; abandon the manual path
        if not still_blocked:
            job.blocked = False
            job.message = "CAPTCHA solved — resuming"
            db.commit()
            print(f"[worker] job {job.id} CAPTCHA solved; resuming", flush=True)
            return True
    job.blocked = False
    db.commit()
    return False


def _with_recovery(db, job, holder, do_work, count_fn):
    """Run do_work(driver) with self-healing retries: rebuild the browser on
    failure (frees memory / clears popup state) and resume. A Radware/perfdrive
    block is IP-based and usually temporary, so back off longer and don't count
    it as a hard no-progress stall. Plain no-progress failures stop after a few
    tries. do_work must be idempotent/resumable; count_fn measures progress."""
    import traceback as _tb

    attempts = 0
    stalls = 0
    while True:
        attempts += 1
        _beat()  # starting an attempt counts as being alive
        # Bail out if the job was stopped externally (admin force-fail / cancel)
        # or the worker is shutting down, so a job can't keep running as a zombie.
        if not _RUNNING:
            raise RuntimeError("worker shutting down")
        try:
            db.refresh(job)
        except Exception:
            pass
        if job.status not in (JOB_RUNNING,):
            raise RuntimeError(f"job stopped externally (status={job.status})")
        before = count_fn()
        try:
            return do_work(holder.get())
        except scraper.IncompleteDownload as exc:
            # Ended short of the site's reported total. Treated as a normal
            # failure so the browser is rebuilt and the download resumes from its
            # checkpoint page; if repeated attempts add no documents the stall
            # detector below ends the job FAILED rather than silently 'done'.
            progressed = count_fn() > before
            print(f"[worker] job {job.id} incomplete: {exc}", flush=True)
            job.message = f"incomplete — retrying: {exc}"
            job.error = str(exc)[:2000]
            db.commit()
            if progressed:
                stalls = 0
            else:
                stalls += 1
            if attempts >= settings.max_download_attempts or stalls >= 3:
                raise
            holder.reset()
            _beat()
            time.sleep(5)
            continue
        except scraper.ProactiveRebuild:
            # Planned memory reset: rebuild the browser and resume from the same
            # page (fast-forward), without counting against the retry/stall limits.
            print(f"[worker] job {job.id} planned browser rebuild to free memory", flush=True)
            job.message = "rebuilding browser to free memory; resuming…"
            db.commit()
            holder.reset()
            attempts -= 1
            _beat()
            time.sleep(2)
            continue
        except Exception as exc:
            progressed = count_fn() > before
            err = "".join(_tb.format_exception_only(type(exc), exc)).strip()
            # Full traceback (file:line) for diagnostics, stored on the job so a
            # repeating failure can be located without Railway log access.
            full = "".join(_tb.format_exception(type(exc), exc, exc.__traceback__)).strip()
            low = err.lower()
            captcha_blocked = (
                "perfdrive" in low or "radware" in low or _driver_is_blocked(holder)
            )
            # A "download produced no file" timeout with no captcha means Radware
            # is throttling the document-zip popup. Treat it (like a captcha
            # block) as transient: back off long and DON'T count it as a hard
            # stall, so the job patiently retries until the IP cools instead of
            # dying after 3 tries. Only a real captcha page gets the manual solve.
            throttled = "produced no file" in low
            # The site being down for everyone is not our failure and cannot be
            # retried away. It must not consume attempts or stalls, or a
            # maintenance window burns every retry and fails the company.
            maintenance = "maintenance" in low or _driver_in_maintenance(holder)
            transient = captcha_blocked or throttled or maintenance

            if maintenance:
                # Don't spend an attempt on a site that is down for everyone.
                attempts -= 1
                notify.slack(
                    "SEDAR Scraper: SEDAR+ is in scheduled maintenance. "
                    "Waiting for it to come back; no action needed.",
                    key="maintenance",
                )
                print(
                    f"[worker] job {job.id} SEDAR+ in maintenance — waiting "
                    f"{int(settings.maintenance_backoff_seconds)}s",
                    flush=True,
                )
                job.message = "SEDAR+ in scheduled maintenance — waiting…"
                db.commit()
                holder.reset()
                _sleep_alive(settings.maintenance_backoff_seconds)
                continue
            if captcha_blocked and settings.manual_captcha and holder._driver is not None:
                # Only wait for a human when there is actually something to
                # solve. Radware also serves a flat "you are a bot" refusal with
                # no challenge on it; pausing 600s for that (and alerting someone
                # to go solve it) just wasted time and cried wolf.
                state = {}
                try:
                    state = sedar_docs.block_state(holder._driver)
                except Exception:
                    pass
                if state.get("solvable"):
                    if _await_manual_solve(db, job, holder):
                        attempts -= 1
                        continue
                else:
                    print(
                        f"[worker] job {job.id} hard-blocked (no challenge to "
                        f"solve): title={state.get('title')!r} "
                        f"snippet={state.get('snippet')!r}",
                        flush=True,
                    )
                    notify.slack(
                        "SEDAR Scraper: hard-blocked by SEDAR+ (no CAPTCHA to "
                        "solve). Waiting for the IP to cool down.",
                        key="hard-block",
                    )
            if progressed:
                stalls = 0
            elif not transient:
                stalls += 1
            if attempts >= settings.max_download_attempts or stalls >= 3:
                raise
            wait = settings.radware_backoff_seconds if transient else 5
            if throttled:
                # Say what actually happened. Calling every missing zip
                # "throttling" was a guess baked in early on, and it made a plain
                # failed download look like a rate limit for the rest of the run.
                kind_msg = f"download did not arrive — retrying in {int(wait)}s"
            elif captcha_blocked:
                kind_msg = f"blocked by SEDAR+ — backing off {int(wait)}s"
            else:
                kind_msg = "recovering after a failure"
            print(f"[worker] job {job.id} {kind_msg} (attempt {attempts}, "
                  f"progressed={progressed}): {err}", flush=True)
            job.message = f"{kind_msg} (attempt {attempts})…"
            # Keep the latest failure text queryable while recovering (not just on
            # a hard failure), so a repeating stall can be diagnosed via the API.
            job.error = full[:2000]
            db.commit()
            holder.reset()
            _sleep_alive(wait)
from . import notify
from . import queue as q
from . import scraper

_RUNNING = True

# Watchdog heartbeat: the main loop stamps _last_beat as it makes progress; a
# daemon thread hard-exits the process if a claimed job goes quiet for too long
# (a freeze the in-process self-heal can't catch), so the start.sh supervisor
# restarts us and requeue_stuck resumes the job.
_last_beat = time.time()
_active_job_id = None


def _beat() -> None:
    global _last_beat
    _last_beat = time.time()


def _watchdog() -> None:
    # Never trip faster than a legitimately slow download can finish, or the
    # supervisor kills work that was about to succeed. A configured 420s sat
    # exactly at download_timeout (240s) + backoff (180s), so the retry was
    # killed every single cycle.
    limit = max(
        settings.watchdog_seconds, settings.download_timeout_seconds + 120
    )
    while _RUNNING:
        time.sleep(30)
        if not settings.watchdog_seconds or _active_job_id is None:
            continue
        idle = time.time() - _last_beat
        if idle > limit:
            print(
                f"[watchdog] job {_active_job_id}: no progress for {int(idle)}s "
                f"(> {int(settings.watchdog_seconds)}s) — hard-exiting to force a restart",
                flush=True,
            )
            os._exit(1)  # supervisor (start.sh) restarts; requeue_stuck resumes


def _stop(*_a):
    global _RUNNING
    _RUNNING = False


class _DriverHolder:
    """Lazily builds the browser and reuses it across jobs; rebuilds on error."""

    def __init__(self):
        self._driver = None

    def get(self):
        if self._driver is None:
            self._driver = scraper.make_driver(settings.staging_dir)
        return self._driver

    def diagnostics(self) -> str:
        """Capture where the browser actually is, so a failure tells us whether
        we hit the Radware block page (validate.perfdrive.com) or a changed UI."""
        if self._driver is None:
            return ""
        try:
            url = self._driver.current_url
            title = self._driver.title
            body = self._driver.find_element("tag name", "body").text[:500]
            return f"\n[where] url={url}\n[where] title={title}\n[where] body<<{body}>>"
        except Exception as e:  # browser may already be dead
            return f"\n[where] diagnostics failed: {e}"

    def reset(self):
        if self._driver is not None:
            try:
                self._driver.quit()
            except Exception:
                pass
            self._driver = None


def _dump_structure(driver) -> str:
    """Summarise the current page: url, title, inputs, buttons, table headers."""
    info = driver.execute_script(
        """
        const txt = el => (el.textContent||'').trim().slice(0,40);
        return {
          url: location.href,
          title: document.title,
          inputs: [...document.querySelectorAll('input,select')]
            .map(i=>[i.tagName, i.getAttribute('name')||'', i.getAttribute('placeholder')||i.getAttribute('aria-label')||''])
            .slice(0,30),
          buttons: [...document.querySelectorAll('button,a.button,input[type=submit]')]
            .map(txt).filter(Boolean).slice(0,30),
          headers: [...document.querySelectorAll('th')].map(txt).filter(Boolean).slice(0,20),
          body: (document.body.innerText||'').replace(/\\s+/g,' ').slice(0,300),
        };
        """
    )
    import json as _j
    return _j.dumps(info, indent=1)


def _probe_urls(driver, params) -> str:
    """Two modes:
      {"urls": [...]}                         -> GET each, report landing+links.
      {"flow": {"bootstrap": url,             -> GET bootstrap (session), then
                "clicks": ["link text", ...], click each link/button by text,
                "search": bool}}              optionally click Search, then dump.
    """
    import time

    if isinstance(params, dict) and params.get("flow"):
        f = params["flow"]
        steps = []
        driver.get(f["bootstrap"])
        time.sleep(10)
        steps.append(f"bootstrap -> {driver.current_url} ({driver.title})")
        for sub in f.get("click_href", []):
            clicked = driver.execute_script(
                """const s=arguments[0];
                   const el=[...document.querySelectorAll('a')]
                     .find(a=>(a.getAttribute('href')||'').includes(s));
                   if(el){el.scrollIntoView({block:'center'});el.click();return true;}
                   return false;""",
                sub,
            )
            time.sleep(9)
            steps.append(f"click_href '{sub}' -> {clicked} -> {driver.current_url}")
        for name, value in (f.get("fill") or {}).items():
            driver.execute_script(
                """const n=arguments[0], v=arguments[1];
                   const el=document.querySelector(`input[name="${n}"]`)
                       || [...document.querySelectorAll('input')]
                            .find(i=>(i.getAttribute('placeholder')||'').toLowerCase().includes(n.toLowerCase()));
                   if(el){el.focus();el.value=v;
                     el.dispatchEvent(new Event('input',{bubbles:true}));
                     el.dispatchEvent(new Event('change',{bubbles:true}));}""",
                name, value,
            )
            time.sleep(6)
            steps.append(f"fill {name}={value}")
        for text in f.get("clicks", []):
            clicked = driver.execute_script(
                """const t=arguments[0].toLowerCase();
                   const els=[...document.querySelectorAll('a,button')];
                   const el=els.find(e=>(e.textContent||'').trim().toLowerCase().includes(t));
                   if(el){el.scrollIntoView();el.click();return true;} return false;""",
                text,
            )
            time.sleep(9)
            steps.append(f"click '{text}' -> {clicked} -> {driver.current_url}")
        if f.get("search"):
            driver.execute_script(
                """const b=[...document.querySelectorAll('button')]
                     .find(e=>(e.textContent||'').trim()==='Search'); if(b)b.click();"""
            )
            time.sleep(9)
            steps.append("clicked Search")
        return "\n".join(steps) + "\n\nSTRUCTURE:\n" + _dump_structure(driver)

    urls = params.get("urls", []) if isinstance(params, dict) else params
    report = []
    for url in urls[:6]:
        try:
            driver.get(url)
            time.sleep(10)
            links = driver.execute_script(
                """return [...document.querySelectorAll('a,button')]
                     .map(a=>[(a.textContent||'').trim().slice(0,40), a.getAttribute('href')||''])
                     .filter(x=>x[0] && /profil|document|search|issuer|record/i.test(x[0]+x[1]))
                     .slice(0,25);"""
            )
            body = driver.find_element("tag name", "body").text[:200].replace("\n", " ")
            report.append(
                f"GET {url}\n  -> {driver.current_url}\n  title={driver.title}\n  body={body}\n  links={links}"
            )
        except Exception as e:
            report.append(f"GET {url} -> ERROR {e}")
    return "\n\n".join(report)


def _run_job(job_id: int, holder: _DriverHolder) -> None:
    with session_scope() as db:
        job = db.get(Job, job_id)
        if job is None:
            return

        def progress(batches, done, total, msg):
            _beat()  # progress = the worker is alive; keep the watchdog happy
            job.batches_done = batches
            job.documents_done = done
            job.total_documents = total
            job.message = msg
            db.commit()

        if job.kind == KIND_PROBE:
            params = json.loads(job.params or "{}")
            job.message = _probe_urls(holder.get(), params)
            db.commit()
            return

        if job.kind == KIND_RESOLVE:
            params = json.loads(job.params or "{}")
            ids = params.get("company_ids") or []
            result = _with_recovery(
                db, job, holder,
                lambda d: scraper.resolve_numbers(db, d, ids, progress=progress),
                count_fn=lambda: 0,
            )
            review = result.get("needs_review") or []
            missed = result.get("not_found") or []
            weak = result.get("low_confidence") or []
            job.message = (
                f"{result.get('resolved', 0)} of {len(ids)} resolved; "
                f"{len(weak)} low confidence; {len(review)} need review; "
                f"{len(missed)} not found"
            )
            # Keep the detail queryable -- a near-miss is exactly what a human
            # has to adjudicate, and it is useless if it only lives in a log.
            # Truncate the lists, not the JSON, so it still parses.
            job.error = _json_dumps({
                "needs_review": review[:40],
                "low_confidence": weak[:40],
                "not_found": missed[:40],
                "totals": {"needs_review": len(review), "low_confidence": len(weak),
                           "not_found": len(missed)},
            })[:4000]
            # Resolution is what makes a freshly imported company publishable at
            # all (the mirror is keyed on the SEDAR number), so push it now --
            # this is the step that re-links files already sitting in R2 to an
            # issuer the catalog had lost.
            _publish_d1(db, job)
            db.commit()
            return

        if job.kind == KIND_ENUMERATE:
            params = json.loads(job.params or "{}")
            result = _with_recovery(
                db, job, holder,
                lambda d: scraper.enumerate_catalog(
                    db, d,
                    profile_type=params.get("profile_type"),
                    max_pages=params.get("max_pages"),
                    progress=progress,
                    should_yield=lambda: (
                        q.has_pending_company_job(db)
                        or q.is_pause_requested(db, job.id)
                    ),
                    # Resume from the last checkpointed page (fast-forward). Read
                    # fresh so a retry after a crash resumes from latest progress.
                    start_page=(job.batches_done or 0),
                ),
                count_fn=lambda: _company_count(db),
            )
            if result.get("yielded"):
                db.refresh(job)  # pick up pause_requested set via the API
                if job.pause_requested:
                    # Manual pause: park it in 'paused' (not requeued) so the
                    # worker is free for other jobs; the UI resumes it later.
                    job.status = JOB_PAUSED
                    job.pause_requested = False
                    job.message = (
                        f"paused at {_company_count(db)} companies — resume from "
                        "the catalog controls"
                    )
                else:
                    # Auto-yield for a waiting download; requeue to resume from the
                    # page we stopped on (fast-forward) once the queue drains.
                    q.enqueue_enumerate(
                        db,
                        profile_type=params.get("profile_type"),
                        max_pages=params.get("max_pages"),
                        start_page=(job.batches_done or 0),
                    )
                    job.message = (
                        f"paused for pending downloads at {_company_count(db)} companies; "
                        "queued a job to resume enumeration"
                    )
            else:
                job.message = (
                    f"catalog now holds {_company_count(db)} companies "
                    f"({result['seen']} seen this pass)"
                )
                _publish_d1(db, job)
            db.commit()
            return

        company = db.get(Company, job.company_id)
        if company is None:
            raise RuntimeError(f"job {job.id} references missing company {job.company_id}")

        only_new = job.kind == KIND_RECHECK
        params = json.loads(job.params or "{}")
        req_max = params.get("max_batches")
        # Per-job cap wins; otherwise the global test-mode default (may be None).
        max_batches = req_max if req_max is not None else settings.default_max_batches
        result = _with_recovery(
            db, job, holder,
            lambda d: scraper.download_company(
                db, d, company, only_new=only_new, max_batches=max_batches, progress=progress,
                # Resume from the checkpoint results page (fast-forward). Read
                # fresh so a retry after a crash resumes from latest progress.
                start_page=(job.batches_done or 0),
            ),
            count_fn=lambda: _doc_count(db, company.id),
        )
        # A company within the sync tolerance reads as a plain success: no
        # INCOMPLETE banner and no caveats, since the remaining gap isn't a file
        # we failed to fetch.
        in_sync = bool(result.get("complete"))
        flags = []
        if not in_sync:
            if result.get("converged"):
                flags.append("converged — remaining gap is unreachable on SEDAR")
            if result.get("short_batches"):
                flags.append(f"{result['short_batches']} short batch(es)")
            if result.get("premature_stop"):
                flags.append("pagination stopped early")
        job.message = (
            f"{result['new_documents']} new doc(s) in {result['batches']} batch(es) "
            f"this pass; {result.get('indexed', 0)}/{result['total_reported']} held"
            + ("" if in_sync else " — INCOMPLETE")
            + (f" ({'; '.join(flags)})" if flags else "")
        )
        db.commit()
        if result.get("new_documents"):
            _publish_filings(db)


def run_forever() -> None:
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    init_db()
    with session_scope() as db:
        n = q.requeue_stuck_jobs(db)
        if n:
            print(f"[worker] requeued {n} stuck job(s) from a previous run")

    print("[worker] started; polling for jobs")
    if settings.watchdog_seconds:
        threading.Thread(target=_watchdog, daemon=True).start()
        print(f"[worker] watchdog armed at {int(settings.watchdog_seconds)}s")
    holder = _DriverHolder()
    global _active_job_id
    try:
        while _RUNNING:
            _beat()
            job_id = None
            with session_scope() as db:
                job = q.claim_next_job(db)
                if job:
                    job_id = job.id
                    kind = job.kind
            if job_id is None:
                _active_job_id = None
                time.sleep(settings.worker_poll_seconds)
                continue

            print(f"[worker] running job {job_id} ({kind})")
            _active_job_id = job_id  # arm the watchdog for this job
            _beat()
            try:
                _run_job(job_id, holder)
                with session_scope() as db:
                    q.finish_job(db, db.get(Job, job_id), ok=True)
                print(f"[worker] job {job_id} done")
            except Exception as exc:  # keep the worker alive across failures
                err = "".join(traceback.format_exception_only(type(exc), exc)).strip()
                diag = holder.diagnostics()  # capture browser state before reset
                err += diag
                low = (err + diag).lower()
                walled = (
                    "perfdrive" in low
                    or "radware" in low
                    or "captcha" in low
                    or "maintenance" in low
                )
                print(f"[worker] job {job_id} FAILED: {err}")
                traceback.print_exc()
                holder.reset()  # browser may be in a bad state; rebuild next job
                if walled:
                    # A bot wall blocks EVERY company, not just this one. Failing
                    # here made the worker grab the next job, hit the same wall,
                    # and burn the entire queue (10 jobs lost in ~35 minutes).
                    # Put the job back and wait for the IP to cool or a human to
                    # solve the challenge.
                    wait = settings.radware_backoff_seconds
                    print(
                        f"[worker] job {job_id} hit a bot wall — requeued; "
                        f"pausing {int(wait)}s before taking more work",
                        flush=True,
                    )
                    with session_scope() as db:
                        jb = db.get(Job, job_id)
                        if jb is not None:
                            jb.status = JOB_QUEUED
                            jb.blocked = False
                            jb.started_at = None
                            jb.message = "blocked by SEDAR+ — waiting to retry"
                            jb.error = err[:2000]
                            db.commit()
                    time.sleep(wait)
                else:
                    with session_scope() as db:
                        jb = db.get(Job, job_id)
                        q.finish_job(db, jb, ok=False, error=err)
                        # Put the company back in line by itself. It resumes from
                        # its checkpoint and re-downloads nothing, so an unattended
                        # queue finishes instead of stalling on the first failure.
                        again = q.auto_retry(db, jb, settings.max_job_retries)
                        if again is not None:
                            print(
                                f"[worker] job {job_id} re-queued automatically "
                                f"as job {again.id}",
                                flush=True,
                            )
            finally:
                _active_job_id = None  # disarm between jobs
    finally:
        holder.reset()
        print("[worker] stopped")


if __name__ == "__main__":
    run_forever()
