"""FastAPI entrypoint: serves the API and the single-page UI.

Run:  uvicorn app.main:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .api import router
from .config import settings
from .db import init_db

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="SEDAR-Scrape", version="0.2.0")
app.include_router(router)


@app.on_event("startup")
def _startup() -> None:
    init_db()
    if settings.enable_inprocess_cron:
        _start_cron()


def _start_cron() -> None:
    """Optional in-process daily recheck. Prefer an external Railway Cron that
    POSTs /api/cron/recheck-all in production; this is for single-box setups."""
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
    except Exception:
        print("[cron] apscheduler not installed; in-process cron disabled")
        return

    from .db import session_scope
    from . import queue as q
    from sqlalchemy import select
    from .models import Company

    def _recheck_all():
        with session_scope() as db:
            for c in db.scalars(select(Company).where(Company.saved.is_(True))):
                q.enqueue_recheck(db, c)
        print("[cron] queued rechecks for saved companies")

    sched = BackgroundScheduler(daemon=True)
    sched.add_job(_recheck_all, "cron", hour=settings.cron_hour, minute=0)
    sched.start()
    print(f"[cron] in-process daily recheck scheduled for {settings.cron_hour:02d}:00")


@app.get("/healthz")
def healthz():
    return {"ok": True}


# Mount the SPA last so /api/* and /healthz win.
app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
