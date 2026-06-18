# Two-process split (e.g. Railway "two services from one repo", or a Procfile
# host). For a single combined process, use ./start.sh instead.
web: uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}
worker: xvfb-run -a -s "-screen 0 1920x1400x24" python -m app.worker
