#!/usr/bin/env bash
# Single-container launcher: a shared virtual display (Xvfb) that runs real,
# non-headless Chrome for the worker AND is exposed over VNC/noVNC so a human can
# solve a Radware CAPTCHA in the live browser when a job gets blocked. Also runs
# the FastAPI web server (which proxies the noVNC websocket to x11vnc).
#
#   Xvfb :99  <-- Chrome (worker) renders here
#     |  x11vnc (localhost:5900)  <-- the app proxies this over /novnc/websockify
#     |  fluxbox (window manager, so the Chrome window is focusable under VNC)
#   worker (DISPLAY=:99) + uvicorn (web)
#
# To split web/worker into separate Railway services, override the start command
# per service instead of using this script (see the Dockerfile header).
set -uo pipefail

PORT="${PORT:-8000}"
export DISPLAY="${DISPLAY:-:99}"
SCREEN="${XVFB_SCREEN:-1920x1400x24}"

echo "[start] Xvfb on $DISPLAY ($SCREEN)"
Xvfb "$DISPLAY" -screen 0 "$SCREEN" -ac +extension RANDR -nolisten tcp \
    >/tmp/xvfb.log 2>&1 &
sleep 3  # give Xvfb a moment to come up before anything uses the display

# Light window manager so the Chrome window is managed/focusable under VNC.
fluxbox >/tmp/fluxbox.log 2>&1 &

# VNC server on the display, bound to localhost only: the ONLY way in is the
# app's proxied websocket (/novnc/websockify). Password-protect it if VNC_PASSWORD
# is set (recommended on a public URL); otherwise run open (dev only).
VNC_ARGS=(-display "$DISPLAY" -forever -shared -rfbport 5900 -localhost -bg -o /tmp/x11vnc.log)
if [ -n "${VNC_PASSWORD:-}" ]; then
  mkdir -p /tmp/vnc
  x11vnc -storepasswd "$VNC_PASSWORD" /tmp/vnc/passwd >/dev/null 2>&1
  VNC_ARGS+=(-rfbauth /tmp/vnc/passwd)
  echo "[start] x11vnc on :5900 (password protected)"
else
  VNC_ARGS+=(-nopw)
  echo "[start] x11vnc on :5900 (NO PASSWORD — set VNC_PASSWORD on a public host)"
fi
x11vnc "${VNC_ARGS[@]}" || echo "[start] WARN: x11vnc failed to start (live view unavailable)"

echo "[start] launching worker supervisor (DISPLAY=$DISPLAY)"
# Supervisor loop: if the worker exits (crash, OOM-kill, or the watchdog's
# hard-exit on a freeze), restart it. On restart requeue_stuck_jobs resumes any
# job left 'running', so a stuck download self-recovers without a manual reboot.
( while true; do
    python -m app.worker
    echo "[start] worker exited (code $?); restarting in 3s" >&2
    sleep 3
  done ) &
WORKER_PID=$!

# On container shutdown, stop the supervisor (and its worker child).
trap 'kill $WORKER_PID 2>/dev/null || true' EXIT

echo "[start] launching web on :$PORT"
exec uvicorn app.main:app --host 0.0.0.0 --port "$PORT"
