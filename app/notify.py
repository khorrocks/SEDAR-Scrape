"""Outbound alerts for things that need a human.

Only one situation currently qualifies: a CAPTCHA wall, which halts the queue
until someone solves it in the live browser view. Everything else the worker can
recover from on its own.

Alerts are rate-limited per key. The recovery loop re-detects a wall on every
retry, so an un-rate-limited notifier would post the same message dozens of times
for a single episode. Sending never raises -- a notification problem must not
take down a download.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.request

from .config import settings

_lock = threading.Lock()
_last_sent: dict[str, float] = {}


def slack(text: str, *, key: str | None = None, cooldown: float | None = None) -> bool:
    """Post ``text`` to the configured Slack webhook. No-op when unset.

    ``key`` groups repeats for rate limiting (defaults to the message itself);
    ``cooldown`` is the minimum seconds between posts for that key.
    """
    url = settings.slack_webhook_url
    if not url:
        return False

    bucket = key or text
    gap = settings.slack_cooldown_seconds if cooldown is None else cooldown
    now = time.time()
    with _lock:
        if now - _last_sent.get(bucket, 0.0) < gap:
            return False
        _last_sent[bucket] = now

    try:
        req = urllib.request.Request(
            url,
            data=json.dumps({"text": text}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
        print(f"[notify] slack alert sent: {text}", flush=True)
        return True
    except Exception as exc:  # never let alerting break the worker
        print(f"[notify] slack post failed: {exc}", flush=True)
        return False
