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
    KIND_DOWNLOAD,
    KIND_ENUMERATE,
    KIND_PROBE,
    KIND_RECHECK,
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
    print(f"[worker] job {job.id} paused for manual CAPTCHA solve "
          f"(up to {int(settings.captcha_wait_seconds)}s)", flush=True)
    deadline = time.time() + settings.captcha_wait_seconds
    while _RUNNING and time.time() < deadline:
        time.sleep(3)
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
        before = count_fn()
        try:
            return do_work(holder.get())
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
            transient = captcha_blocked or throttled

            if captcha_blocked and settings.manual_captcha and holder._driver is not None:
                # Manual solve: if a human clears the captcha in the live view,
                # retry on the SAME browser (a reset discards the cleared session)
                # without counting the pause against the attempt cap.
                if _await_manual_solve(db, job, holder):
                    attempts -= 1
                    continue
            if progressed:
                stalls = 0
            elif not transient:
                stalls += 1
            if attempts >= settings.max_download_attempts or stalls >= 3:
                raise
            wait = settings.radware_backoff_seconds if transient else 5
            if throttled:
                kind_msg = f"SEDAR/Radware throttling downloads — backing off {int(wait)}s"
            elif captcha_blocked:
                kind_msg = f"Radware throttling the IP — backing off {int(wait)}s"
            else:
                kind_msg = "recovering after a failure"
            print(f"[worker] job {job.id} {kind_msg} (attempt {attempts}, "
                  f"progressed={progressed}): {err}", flush=True)
            job.message = f"{kind_msg} (attempt {attempts})…"
            # Keep the latest failure text queryable while recovering (not just on
            # a hard failure), so a repeating stall can be diagnosed via the API.
            job.error = err[:2000]
            db.commit()
            holder.reset()
            time.sleep(wait)
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
    while _RUNNING:
        time.sleep(30)
        if not settings.watchdog_seconds or _active_job_id is None:
            continue
        idle = time.time() - _last_beat
        if idle > settings.watchdog_seconds:
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
            self._driver = scraper.make_driver(settings.download_dir)
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
        job.message = (
            f"{result['new_documents']} new doc(s) in {result['batches']} batch(es) "
            f"this pass; {result['total_reported']} reported on site"
        )
        db.commit()


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
                err += holder.diagnostics()  # capture browser state before reset
                print(f"[worker] job {job_id} FAILED: {err}")
                traceback.print_exc()
                holder.reset()  # browser may be in a bad state; rebuild next job
                with session_scope() as db:
                    q.finish_job(db, db.get(Job, job_id), ok=False, error=err)
            finally:
                _active_job_id = None  # disarm between jobs
    finally:
        holder.reset()
        print("[worker] stopped")


if __name__ == "__main__":
    run_forever()
