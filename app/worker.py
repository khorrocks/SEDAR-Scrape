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
from .models import (
    KIND_DOWNLOAD,
    KIND_ENUMERATE,
    KIND_RECHECK,
    Company,
    Job,
)
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
        result = scraper.download_company(
            db, holder.get(), company, only_new=only_new, progress=progress
        )
        job.message = (
            f"{result['new_documents']} new doc(s) in {result['batches']} batch(es); "
            f"{result['total_reported']} reported on site"
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
