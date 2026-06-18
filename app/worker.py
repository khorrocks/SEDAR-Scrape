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
import signal
import time
import traceback

from .config import settings
from .db import init_db, session_scope
from sqlalchemy import func, select

from .models import (
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
from . import queue as q
from . import scraper

_RUNNING = True


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
            n = scraper.enumerate_catalog(
                db, holder.get(),
                profile_type=params.get("profile_type", "Company"),
                max_pages=params.get("max_pages"),
            )
            job.message = f"catalog upserted {n} companies"
            db.commit()
            return

        company = db.get(Company, job.company_id)
        if company is None:
            raise RuntimeError(f"job {job.id} references missing company {job.company_id}")

        only_new = job.kind == KIND_RECHECK
        # The browser degrades after several big-zip downloads (popup churn /
        # memory). Retry with a FRESH browser on failure; download_company
        # resumes by skipping documents already saved, so each attempt makes
        # forward progress until the company is complete.
        attempts = 0
        stalls = 0
        while True:
            attempts += 1
            saved_before = _doc_count(db, company.id)
            try:
                result = scraper.download_company(
                    db, holder.get(), company, only_new=only_new, progress=progress
                )
                break
            except Exception as exc:
                progressed = _doc_count(db, company.id) > saved_before
                stalls = 0 if progressed else stalls + 1
                err = "".join(traceback.format_exception_only(type(exc), exc)).strip()
                # Give up if we've made no progress several attempts in a row
                # (a genuine failure, not just browser degradation).
                if attempts >= 12 or stalls >= 3:
                    raise
                print(f"[worker] job {job.id} batch failure (attempt {attempts}, "
                      f"progressed={progressed}); rebuilding browser and resuming: {err}")
                job.message = f"recovering after a batch failure (attempt {attempts})…"
                db.commit()
                holder.reset()  # fresh Chrome frees memory and clears popup state
                time.sleep(5)
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
    holder = _DriverHolder()
    try:
        while _RUNNING:
            job_id = None
            with session_scope() as db:
                job = q.claim_next_job(db)
                if job:
                    job_id = job.id
                    kind = job.kind
            if job_id is None:
                time.sleep(settings.worker_poll_seconds)
                continue

            print(f"[worker] running job {job_id} ({kind})")
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
        holder.reset()
        print("[worker] stopped")


if __name__ == "__main__":
    run_forever()
