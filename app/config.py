"""Runtime configuration, all environment-driven so the same image runs on
Railway, Render, Fly, a VPS, or locally."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int | None) -> int | None:
    v = os.getenv(name)
    return int(v) if v not in (None, "") else default


@dataclass
class Settings:
    # Where the SQLite file / downloaded files live. On Railway this should be a
    # mounted volume path (e.g. /data) so files survive redeploys.
    data_dir: Path = Path(os.getenv("DATA_DIR", "./data"))

    # SQLAlchemy URL. Defaults to SQLite under data_dir; set DATABASE_URL to a
    # Postgres URL (Railway plugin) in production.
    database_url: str = os.getenv("DATABASE_URL", "")

    # --- Chrome / scraper knobs (forwarded to sedar.browser.BrowserConfig) ---
    chrome_binary: str | None = os.getenv("CHROME_BINARY") or None
    chromedriver_binary: str | None = os.getenv("CHROMEDRIVER") or None
    chrome_version: int | None = _int("CHROME_VERSION", None)
    headless: bool = _bool("HEADLESS", False)  # leave False; Radware blocks headless
    ignore_cert_errors: bool = _bool("IGNORE_CERT_ERRORS", False)

    # --- Worker / queue ---
    worker_poll_seconds: float = float(os.getenv("WORKER_POLL_SECONDS", "5"))
    # Polite pause between document batches (per-page zip downloads).
    batch_pause_seconds: float = float(os.getenv("BATCH_PAUSE_SECONDS", "3"))
    # Per-batch (per 30-doc zip) download timeout.
    download_timeout_seconds: float = float(os.getenv("DOWNLOAD_TIMEOUT_SECONDS", "180"))

    # --- Cron ---
    # If true, the worker runs an in-process daily scheduler to re-check saved
    # companies for new documents. On Railway you can instead use a Cron service
    # that POSTs /api/cron/recheck-all; leave this False then.
    enable_inprocess_cron: bool = _bool("ENABLE_INPROCESS_CRON", False)
    cron_hour: int = int(os.getenv("CRON_HOUR", "3"))  # 24h local time

    @property
    def download_dir(self) -> Path:
        return self.data_dir / "downloads"

    @property
    def resolved_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        self.data_dir.mkdir(parents=True, exist_ok=True)
        return f"sqlite:///{(self.data_dir / 'sedar.db').resolve()}"


settings = Settings()
settings.data_dir.mkdir(parents=True, exist_ok=True)
settings.download_dir.mkdir(parents=True, exist_ok=True)
