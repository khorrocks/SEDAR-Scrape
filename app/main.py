"""FastAPI entrypoint: serves the API and the single-page UI.

Run:  uvicorn app.main:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from fastapi import FastAPI, WebSocket
from fastapi.staticfiles import StaticFiles

from .api import router
from .config import settings
from .db import init_db

STATIC_DIR = Path(__file__).parent / "static"

# noVNC web assets (installed by the Docker image); the live browser view is
# served from here and the VNC stream is proxied over the websocket below.
NOVNC_DIR = os.getenv("NOVNC_DIR", "/usr/share/novnc")
# Where x11vnc listens inside the container (see start.sh). localhost-only, so
# the only way in is through this app's proxied websocket.
VNC_HOST = os.getenv("VNC_HOST", "127.0.0.1")
VNC_PORT = int(os.getenv("VNC_PORT", "5900"))

app = FastAPI(title="SEDAR-Scrape", version="0.2.0")
app.include_router(router)


@app.websocket("/novnc/websockify")
async def _vnc_bridge(ws: WebSocket) -> None:
    """Bridge a noVNC websocket to the local x11vnc TCP socket (a minimal
    websockify). The RFB stream is opaque bytes; we just pipe both directions.
    Access control is the VNC password enforced by x11vnc (set VNC_PASSWORD)."""
    # noVNC requests the 'binary' subprotocol; echo it back if offered.
    offered = ws.headers.get("sec-websocket-protocol", "")
    subproto = "binary" if "binary" in offered else None
    await ws.accept(subprotocol=subproto)
    try:
        reader, writer = await asyncio.open_connection(VNC_HOST, VNC_PORT)
    except Exception:
        await ws.close(code=1011)
        return

    async def ws_to_tcp() -> None:
        try:
            while True:
                data = await ws.receive_bytes()
                writer.write(data)
                await writer.drain()
        except Exception:
            pass

    async def tcp_to_ws() -> None:
        try:
            while True:
                data = await reader.read(65536)
                if not data:
                    break
                await ws.send_bytes(data)
        except Exception:
            pass

    t1 = asyncio.create_task(ws_to_tcp())
    t2 = asyncio.create_task(tcp_to_ws())
    try:
        await asyncio.wait({t1, t2}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in (t1, t2):
            t.cancel()
        try:
            writer.close()
        except Exception:
            pass
        try:
            await ws.close()
        except Exception:
            pass


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


@app.get("/api/vnc/status")
def vnc_status():
    """Whether the live browser view is available (noVNC assets present)."""
    return {"available": os.path.isdir(NOVNC_DIR)}


# Mount noVNC assets (if present) before the SPA so /novnc/* resolves to the
# live-view client. Guarded so local dev without the assets still boots.
if os.path.isdir(NOVNC_DIR):
    app.mount("/novnc", StaticFiles(directory=NOVNC_DIR, html=True), name="novnc")

# Mount the SPA last so /api/*, /novnc/*, and /healthz win.
app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
