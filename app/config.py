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
    batch_pause_seconds: float = float(os.getenv("BATCH_PAUSE_SECONDS", "20"))
    # On a Radware block, how long the worker waits before retrying (the block
    # is IP-based and usually clears after the IP cools down).
    radware_backoff_seconds: float = float(os.getenv("RADWARE_BACKOFF_SECONDS", "180"))
    # Max self-heal attempts for one company download before giving up.
    max_download_attempts: int = int(os.getenv("MAX_DOWNLOAD_ATTEMPTS", "15"))
    # Treat a company as in sync when it is within this many documents of the
    # total SEDAR+ reports. Small gaps are usually a counting artifact on their
    # side (duplicate rows that collapse to one filing) rather than a file we
    # failed to fetch -- chasing them just re-walks every page forever.
    sync_tolerance: int = int(os.getenv("SYNC_TOLERANCE", "5"))
    # How many times a company's download is automatically re-queued after a
    # failure before it needs a human. Bot walls, browser wedges and timeouts are
    # routine here, and a company that stops short should get itself back in line
    # rather than waiting for someone to notice and press a button.
    max_job_retries: int = int(os.getenv("MAX_JOB_RETRIES", "6"))
    # Per-batch (per 30-doc zip) download timeout. A healthy batch lands in ~40s,
    # so 240s is already very generous; the old 600s just meant a flaky batch
    # blocked the queue for ten minutes before the retry could start.
    download_timeout_seconds: float = float(os.getenv("DOWNLOAD_TIMEOUT_SECONDS", "240"))
    # Global "test mode": cap every download to this many 30-doc batches. Unset
    # (None) means download everything. A per-request max_batches overrides this.
    default_max_batches: int | None = _int("MAX_BATCHES", None)
    # Fetch only the NEW documents on a partially-held page by ticking individual
    # row checkboxes. OFF by default: SEDAR+ re-renders the results table on every
    # tick and discards the previous ones, so the selection never holds (observed:
    # 0 of 29 kept) AND it leaves the page in a state where the page-level
    # "All documents" checkbox no longer works either -- the archive came back
    # with 1 file instead of 29, wedging the run. Whole-page downloads re-fetch a
    # few already-held documents, which dedup discards.
    selective_download: bool = _bool("SELECTIVE_DOWNLOAD", False)
    # Proactively rebuild the browser after this many downloaded batches to bound
    # Chrome's memory on long downloads (the worker resumes from the same page via
    # fast-forward). Prevents the container OOM-killing the worker mid-download.
    # 0 disables.
    download_rebuild_every_batches: int = int(os.getenv("DOWNLOAD_REBUILD_EVERY", "5"))

    # --- Watchdog ---
    # If a job makes no progress for this long, the worker hard-exits so the
    # start.sh supervisor restarts it (and requeue_stuck resumes the job). Guards
    # against a frozen browser/worker that the in-process self-heal can't catch.
    # Set generously above the per-batch download timeout; 0 disables it.
    watchdog_seconds: float = float(os.getenv("WATCHDOG_SECONDS", "420"))

    # --- Admin ---
    # Token gating destructive admin endpoints (e.g. POST /api/admin/reset).
    # Unset => those endpoints are disabled (return 403).
    admin_token: str | None = os.getenv("ADMIN_TOKEN") or None

    # --- Simple login gate (single hardcoded user, supplied via env) ---
    # When both are set, the whole site (UI + API + live view) requires signing
    # in; a session cookie keeps you logged in. Unset => no gate (e.g. local dev).
    # Kept in env (not source) so the password isn't committed to the repo.
    auth_username: str | None = os.getenv("AUTH_USERNAME") or None
    auth_password: str | None = os.getenv("AUTH_PASSWORD") or None

    # --- Manual CAPTCHA solving (noVNC live browser view) ---
    # When True, a Radware/captcha block pauses the job (keeping the SAME
    # browser) and waits for a human to solve it via the live noVNC view,
    # instead of failing. Falls back to auto-backoff if nobody solves in time.
    manual_captcha: bool = _bool("MANUAL_CAPTCHA", True)
    # How long to wait for a human to solve a captcha before giving up and
    # letting the normal recovery/backoff take over.
    captcha_wait_seconds: float = float(os.getenv("CAPTCHA_WAIT_SECONDS", "600"))

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
        """Where completed archives are KEPT in local (non-R2) mode."""
        return self.data_dir / "downloads"

    @property
    def staging_dir(self) -> Path:
        """Where Chrome writes the in-flight download.

        With R2 configured an archive is inspected, uploaded and deleted within
        seconds, so staging has no reason to sit on the persistent volume -- and
        putting it there meant transient files (plus debris from failed
        downloads) competed for space with the database. Staging on ephemeral
        container disk leaves the volume holding essentially just sedar.db.
        Without R2 the archives are the only copy, so they stay on the volume.
        """
        if self.r2_enabled:
            return Path(os.getenv("STAGING_DIR", "/tmp/sedar-downloads"))
        return self.download_dir

    @property
    def auth_enabled(self) -> bool:
        return bool(self.auth_username and self.auth_password)

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
settings.staging_dir.mkdir(parents=True, exist_ok=True)
