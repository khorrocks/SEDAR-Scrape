"""Cloudflare R2 (S3-compatible) storage helpers.

R2 is the source of truth for scraped batch zips. The layout is:

    <bucket>/<prefix>/<exchange>-<ticker>/raw-data/<timestamp>_batchNNNN.zip
    e.g.  smallcap-kb/kb/tsxv-sprq/raw-data/20260724T120000Z_batch0001.zip

The web app browses this space (the R2 viewer) and serves objects via short-lived
presigned URLs. The worker uploads each batch zip here, then deletes only its own
local staging copy.

**R2 is append-only.** This platform uploads and reads; it must never delete an
object, a prefix, or a bucket. Scraped filings are the product of long,
rate-limited runs against SEDAR+, so an accidental delete is expensive and
unrecoverable. That rule is enforced in code, not just by convention: the client
is wrapped so destructive methods cannot be called, and a botocore hook refuses
any Delete* (or lifecycle-expiry) operation before it is signed. Removing data
from R2 is a deliberate, out-of-band action for a human with the dashboard or a
separate tool.

All paths passed in from HTTP are *relative to the root prefix* (``kb/``) and are
sanitised so a request can never escape it or reach another bucket.

If R2 credentials are absent (``settings.r2_enabled`` is False), the helpers here
are unused and the app keeps everything on local disk.
"""

from __future__ import annotations

import threading
from pathlib import PurePosixPath
from typing import Any

from .config import settings

# boto3 is imported lazily so DB-only entrypoints (and environments without the
# dependency installed) don't pay for it.
_client: Any = None
_client_lock = threading.Lock()


class R2Disabled(RuntimeError):
    """Raised when an R2 operation is attempted without credentials configured."""


class R2DeleteForbidden(RuntimeError):
    """Raised if anything in this process tries to remove data from R2.

    R2 is append-only by policy: this platform uploads and reads, and must never
    delete an object, a prefix, or a bucket. Scraped filings are the product of
    long, rate-limited runs against SEDAR+, so an accidental delete is expensive
    and unrecoverable.
    """


# Operations that could destroy stored data. Anything named Delete* is refused;
# the lifecycle calls are listed explicitly because they don't delete directly --
# they install a server-side rule that expires objects later, which is the same
# outcome with a delay. AbortMultipartUpload is deliberately NOT here: it only
# discards the parts of an in-flight upload this process itself started, and
# never touches a stored object (boto3 needs it to clean up a failed upload).
_FORBIDDEN_OPS = frozenset(
    {
        "PutBucketLifecycle",
        "PutBucketLifecycleConfiguration",
        "PutLifecycleConfiguration",
        "PutBucketReplication",
    }
)

# Client methods this process is allowed to reach at all.
_ALLOWED_METHODS = frozenset(
    {
        "upload_file",
        "upload_fileobj",
        "put_object",
        "get_object",
        "head_object",
        "head_bucket",
        "list_objects_v2",
        "get_paginator",
        "generate_presigned_url",
        "meta",
    }
)


def _veto_destructive(model=None, **_kwargs) -> None:
    """botocore hook: refuse a destructive S3 call before it is ever signed."""
    op = getattr(model, "name", "") or ""
    if op.startswith("Delete") or op in _FORBIDDEN_OPS:
        raise R2DeleteForbidden(
            f"R2 is append-only: the '{op}' operation is blocked by this platform"
        )


class _WriteOnlyClient:
    """Exposes only the non-destructive parts of the boto3 S3 client.

    Two layers on purpose. This proxy makes a delete impossible to *call*
    (``client.delete_object`` does not resolve), and the botocore hook on the
    wrapped client refuses the request even if the underlying client is reached
    some other way -- so neither a future code change nor a library path can
    quietly remove data.
    """

    __slots__ = ("_inner",)

    def __init__(self, inner):
        object.__setattr__(self, "_inner", inner)

    def __getattr__(self, name: str):
        if name not in _ALLOWED_METHODS:
            raise R2DeleteForbidden(
                f"R2 client method '{name}' is not permitted; this platform may "
                "only upload and read from R2"
            )
        return getattr(object.__getattribute__(self, "_inner"), name)

    def __setattr__(self, name, value):  # keep the proxy immutable
        raise R2DeleteForbidden("the R2 client is read-only and cannot be modified")


def _build_client():
    global _client
    if not settings.r2_enabled:
        raise R2Disabled("R2 credentials are not configured (set R2_ACCOUNT_ID etc.)")
    if _client is None:
        with _client_lock:
            if _client is None:
                import boto3
                from botocore.config import Config

                raw = boto3.client(
                    "s3",
                    endpoint_url=settings.r2_endpoint_url,
                    aws_access_key_id=settings.r2_access_key_id,
                    aws_secret_access_key=settings.r2_secret_access_key,
                    region_name="auto",
                    config=Config(signature_version="s3v4", retries={"max_attempts": 3}),
                )
                # Fires for every S3 operation on this client (botocore events are
                # hierarchical, so the service-level name catches them all).
                raw.meta.events.register("before-parameter-build.s3", _veto_destructive)
                _client = _WriteOnlyClient(raw)
    return _client


# --------------------------------------------------------------------------- #
# Key helpers
# --------------------------------------------------------------------------- #
def _clean_rel(rel: str) -> str:
    """Normalise a viewer-supplied path relative to the root prefix, rejecting
    any attempt to traverse above it. Returns a clean relative path (may be '')."""
    rel = (rel or "").lstrip("/")
    parts = []
    for seg in PurePosixPath(rel).parts:
        if seg in ("", "."):
            continue
        if seg == "..":
            raise ValueError("path traversal is not allowed")
        parts.append(seg)
    return "/".join(parts)


def full_key(rel: str) -> str:
    """Absolute object key from a path relative to the root prefix."""
    rel = _clean_rel(rel)
    return f"{settings.r2_root_prefix}{rel}"


def raw_data_key(slug: str, filename: str) -> str:
    """Object key for a scraped batch zip: <prefix>/<slug>/raw-data/<filename>."""
    slug = _clean_rel(slug)
    filename = PurePosixPath(filename).name
    return f"{settings.r2_root_prefix}{slug}/raw-data/{filename}"


def to_relative(key: str) -> str | None:
    """Strip the root prefix from an absolute key, or None if it's outside it."""
    root = settings.r2_root_prefix
    if root and not key.startswith(root):
        return None
    return key[len(root):]


# --------------------------------------------------------------------------- #
# Operations
# --------------------------------------------------------------------------- #
def upload_file(local_path, key: str, content_type: str = "application/zip") -> str:
    """Upload a local file to ``key`` and return the key."""
    c = _build_client()
    c.upload_file(
        str(local_path),
        settings.r2_bucket,
        key,
        ExtraArgs={"ContentType": content_type},
    )
    return key


def presigned_url(key: str, expires: int | None = None) -> str:
    """A short-lived GET URL for an absolute object key."""
    c = _build_client()
    return c.generate_presigned_url(
        "get_object",
        Params={"Bucket": settings.r2_bucket, "Key": key},
        ExpiresIn=int(expires or settings.r2_url_expiry_seconds),
    )


def object_exists(key: str) -> bool:
    c = _build_client()
    try:
        c.head_object(Bucket=settings.r2_bucket, Key=key)
        return True
    except Exception:
        return False


def list_dir(rel: str = "") -> dict:
    """Folder-style listing of ``<root>/<rel>``: immediate subfolders + files.

    Returns paths *relative to the root prefix* so the viewer never sees the
    bucket/prefix plumbing. Handles pagination for large folders.
    """
    c = _build_client()
    rel = _clean_rel(rel)
    prefix = full_key(rel)
    if prefix and not prefix.endswith("/"):
        prefix += "/"

    folders: list[dict] = []
    files: list[dict] = []
    paginator = c.get_paginator("list_objects_v2")
    for page in paginator.paginate(
        Bucket=settings.r2_bucket, Prefix=prefix, Delimiter="/"
    ):
        for cp in page.get("CommonPrefixes", []):
            key = cp["Prefix"]
            name = key[len(prefix):].rstrip("/")
            if name:
                folders.append({"name": name, "path": to_relative(key) or ""})
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key == prefix:  # the folder placeholder object itself
                continue
            name = key[len(prefix):]
            if not name or "/" in name:
                continue
            files.append(
                {
                    "name": name,
                    "path": to_relative(key) or "",
                    "size": obj.get("Size", 0),
                    "last_modified": obj.get("LastModified").isoformat()
                    if obj.get("LastModified")
                    else None,
                }
            )
    folders.sort(key=lambda f: f["name"].lower())
    files.sort(key=lambda f: f["name"].lower())
    return {"prefix": rel, "folders": folders, "files": files}
