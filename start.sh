#!/usr/bin/env bash
# Single-container launcher: run the queue worker (under a virtual display so
# real, non-headless Chrome works) AND the web server. The worker MUST run under
# Xvfb -- Radware blocks headless Chrome.
#
# To split web/worker into separate Railway services, set each service's start
# command instead of using this script:
#   web    : uvicorn app.main:app --host 0.0.0.0 --port $PORT
#   worker : xvfb-run -a -s "-screen 0 1920x1400x24" python -m app.worker
set -euo pipefail

PORT="${PORT:-8000}"

echo "[start] launching worker under Xvfb"
xvfb-run -a -s "-screen 0 1920x1400x24" python -m app.worker &
WORKER_PID=$!

# If the worker dies, take the container down so the platform restarts it.
trap 'kill $WORKER_PID 2>/dev/null || true' EXIT

echo "[start] launching web on :$PORT"
exec uvicorn app.main:app --host 0.0.0.0 --port "$PORT"
