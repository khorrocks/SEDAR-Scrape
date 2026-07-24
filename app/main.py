"""FastAPI entrypoint: serves the API and the single-page UI.

Run:  uvicorn app.main:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
from pathlib import Path

from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
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

# --------------------------------------------------------------------------- #
# Login gate (single hardcoded user via env; a session cookie keeps you in)
# --------------------------------------------------------------------------- #
SESSION_COOKIE = "sedar_session"
# Paths reachable without a session (health check + the login flow itself).
_OPEN_PATHS = {"/healthz", "/login", "/api/login", "/api/logout", "/favicon.ico"}


def _session_token() -> str:
    """A constant token derived from the credentials; the login cookie must
    equal this. Not a real signed session — enough for a single hardcoded user."""
    raw = f"{settings.auth_username}:{settings.auth_password}:sedar-scrape-v1"
    return hashlib.sha256(raw.encode()).hexdigest()


def _has_valid_session(token: str | None) -> bool:
    return bool(token) and hmac.compare_digest(token, _session_token())


@app.middleware("http")
async def _auth_gate(request: Request, call_next):
    if not settings.auth_enabled or request.url.path in _OPEN_PATHS:
        return await call_next(request)
    if _has_valid_session(request.cookies.get(SESSION_COOKIE)):
        return await call_next(request)
    # Not signed in: JSON 401 for API calls, redirect to login for page navigations.
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": "authentication required"}, status_code=401)
    return RedirectResponse("/login", status_code=302)


_LOGIN_PAGE = """<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1">
<title>Sign in · SEDAR-Scrape</title><style>
:root{--bg:#0f1419;--panel:#1a212b;--panel2:#222c38;--border:#2c3744;--text:#e6edf3;--muted:#8b98a5;--accent:#3b82f6;--err:#ef4444}
*{box-sizing:border-box}body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
background:var(--bg);color:var(--text);font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
form{background:var(--panel);border:1px solid var(--border);border-radius:14px;padding:28px;width:min(360px,92vw);
display:flex;flex-direction:column;gap:12px;box-shadow:0 20px 50px rgba(0,0,0,.4)}
h1{font-size:20px;margin:0 0 8px;letter-spacing:.5px}h1 span{color:var(--accent)}
input{padding:12px 13px;border-radius:9px;border:1px solid var(--border);background:var(--panel2);color:var(--text);font-size:14px}
input:focus{outline:none;border-color:var(--accent)}
button{padding:12px;border:none;border-radius:9px;background:var(--accent);color:#fff;font-size:14px;font-weight:600;cursor:pointer}
button:hover{filter:brightness(1.1)}.err{color:var(--err);font-size:13px;min-height:18px}</style></head>
<body><form id=f><h1>SEDAR<span>-Scrape</span></h1>
<input id=u type=email placeholder=Email autocomplete=username required>
<input id=p type=password placeholder=Password autocomplete=current-password required>
<button type=submit>Sign in</button><div class=err id=err></div></form><script>
document.getElementById('f').addEventListener('submit',async function(e){e.preventDefault();
var err=document.getElementById('err');err.textContent='';
try{var r=await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},
body:JSON.stringify({username:document.getElementById('u').value,password:document.getElementById('p').value})});
if(r.ok){location.href='/';}else{err.textContent='Invalid email or password.';}}
catch(_){err.textContent='Something went wrong. Try again.';}});</script></body></html>"""


@app.get("/login", response_class=HTMLResponse)
def login_page():
    if not settings.auth_enabled:
        return RedirectResponse("/", status_code=302)
    return HTMLResponse(_LOGIN_PAGE)


@app.post("/api/login")
async def login(request: Request):
    if not settings.auth_enabled:
        return JSONResponse({"ok": True})
    try:
        data = await request.json()
    except Exception:
        data = {}
    user = (data.get("username") or "").strip()
    pw = data.get("password") or ""
    ok = hmac.compare_digest(user, settings.auth_username or "") and hmac.compare_digest(
        pw, settings.auth_password or ""
    )
    if not ok:
        return JSONResponse({"detail": "invalid credentials"}, status_code=401)
    resp = JSONResponse({"ok": True})
    resp.set_cookie(
        SESSION_COOKIE, _session_token(),
        httponly=True, secure=True, samesite="lax", max_age=60 * 60 * 24 * 30,
    )
    return resp


@app.post("/api/logout")
def logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(SESSION_COOKIE)
    return resp


@app.get("/api/auth/status")
def auth_status(request: Request):
    return {"enabled": settings.auth_enabled, "user": settings.auth_username if settings.auth_enabled else None}


app.include_router(router)


@app.websocket("/novnc/websockify")
async def _vnc_bridge(ws: WebSocket) -> None:
    """Bridge a noVNC websocket to the local x11vnc TCP socket (a minimal
    websockify). The RFB stream is opaque bytes; we just pipe both directions.
    Access control is the VNC password enforced by x11vnc (set VNC_PASSWORD)."""
    # Gate the live-view stream behind the login session (the HTTP middleware
    # doesn't see websocket connections, so check the cookie here too).
    if settings.auth_enabled and not _has_valid_session(ws.cookies.get(SESSION_COOKIE)):
        await ws.close(code=1008)
        return
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
