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
    # Polite pause between document batches (per-page zip downloads). Slower
    # pacing reduces how quickly we trip Radware's rate limiting.
    batch_pause_seconds: float = float(os.getenv("BATCH_PAUSE_SECONDS", "8"))
    # On a Radware block, how long the worker waits before retrying (the block
    # is IP-based and usually clears after the IP cools down).
    radware_backoff_seconds: float = float(os.getenv("RADWARE_BACKOFF_SECONDS", "180"))
    # Max self-heal attempts for one company download before giving up.
    max_download_attempts: int = int(os.getenv("MAX_DOWNLOAD_ATTEMPTS", "15"))
    # Per-batch (per 30-doc zip) download timeout.
    download_timeout_seconds: float = float(os.getenv("DOWNLOAD_TIMEOUT_SECONDS", "180"))
    # Global "test mode": cap every download to this many 30-doc batches. Unset
    # (None) means download everything. A per-request max_batches overrides this.
    default_max_batches: int | None = _int("MAX_BATCHES", None)

    # --- Admin ---
    # Token gating destructive admin endpoints (e.g. POST /api/admin/reset).
    # Unset => those endpoints are disabled (return 403).
    admin_token: str | None = os.getenv("ADMIN_TOKEN") or None

    # --- Cloudflare R2 (S3-compatible object storage) ---
    # Scraped batch zips land in R2 under <prefix>/<exchange>-<ticker>/raw-data/.
    # Leave the credentials unset to keep everything on local disk instead.
    r2_account_id: str | None = os.getenv("R2_ACCOUNT_ID") or None
    r2_access_key_id: str | None = os.getenv("R2_ACCESS_KEY_ID") or None
    r2_secret_access_key: str | None = os.getenv("R2_SECRET_ACCESS_KEY") or None
    # Bucket + key prefix. The R2 viewer is rooted at "<bucket>/<prefix>".
    r2_bucket: str = os.getenv("R2_BUCKET", "smallcap-kb")
    r2_prefix: str = os.getenv("R2_PREFIX", "kb/")
    # Override the endpoint if you use a custom/jurisdiction-specific R2 host;
    # otherwise it is derived from the account id.
    r2_endpoint: str | None = os.getenv("R2_ENDPOINT") or None
    # How long presigned view/download links stay valid.
    r2_url_expiry_seconds: int = int(os.getenv("R2_URL_EXPIRY_SECONDS", "3600"))

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
    def r2_enabled(self) -> bool:
        """True only when all three credentials are present; otherwise the app
        falls back to local-disk storage."""
        return bool(
            self.r2_account_id
            and self.r2_access_key_id
            and self.r2_secret_access_key
        )

    @property
    def r2_endpoint_url(self) -> str:
        return self.r2_endpoint or f"https://{self.r2_account_id}.r2.cloudflarestorage.com"

    @property
    def r2_root_prefix(self) -> str:
        """The bucket-relative prefix the viewer is rooted at, e.g. 'kb/'."""
        p = (self.r2_prefix or "").strip("/")
        return f"{p}/" if p else ""

    @property
    def resolved_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        self.data_dir.mkdir(parents=True, exist_ok=True)
        return f"sqlite:///{(self.data_dir / 'sedar.db').resolve()}"


settings = Settings()
settings.data_dir.mkdir(parents=True, exist_ok=True)
settings.download_dir.mkdir(parents=True, exist_ok=True)
