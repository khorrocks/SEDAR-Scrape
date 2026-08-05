"""Publish the issuer catalog to Cloudflare D1.

The local database stays the source of truth. This pushes a read-only mirror so
the Cloudflare-side stack can join against `name -> SEDAR number` directly
instead of asking for a CSV. Deliberately one-way and out-of-band:

  * autocomplete keeps hitting the local DB, so a keystroke never waits on a
    network call and a D1 outage cannot break the add-form;
  * `documents`, `batches` and `jobs` keep their foreign keys into the local
    `companies` table, so nothing has to be re-keyed;
  * a failed publish is a stale mirror, never a broken scraper.

Unconfigured (no token/database) it is a no-op, like R2 without credentials.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime, timezone

from .config import settings

_API = "https://api.cloudflare.com/client/v4/accounts/{acct}/d1/database/{db}/query"

# Mirrors what /catalog/export emits. sedar_number is the natural key: it is
# SEDAR's own identifier and the column the downstream join needs.
_CREATE = """
CREATE TABLE IF NOT EXISTS {table} (
  sedar_number      TEXT PRIMARY KEY,
  name              TEXT,
  jurisdiction      TEXT,
  profile_type      TEXT,
  in_default        TEXT,
  cease_trade_order TEXT,
  updated_at        TEXT
)
"""

_UPSERT = """
INSERT INTO {table}
  (sedar_number, name, jurisdiction, profile_type, in_default, cease_trade_order, updated_at)
VALUES {values}
ON CONFLICT(sedar_number) DO UPDATE SET
  name = excluded.name,
  jurisdiction = excluded.jurisdiction,
  profile_type = excluded.profile_type,
  in_default = excluded.in_default,
  cease_trade_order = excluded.cease_trade_order,
  updated_at = excluded.updated_at
"""


class D1Disabled(RuntimeError):
    """Raised when a D1 operation is attempted without configuration."""


def enabled() -> bool:
    return bool(settings.d1_api_token and settings.d1_database_id and settings.d1_account_id)


def _query(sql: str, params: list | None = None) -> dict:
    if not enabled():
        raise D1Disabled("D1 is not configured (set D1_API_TOKEN and D1_DATABASE_ID)")
    url = _API.format(acct=settings.d1_account_id, db=settings.d1_database_id)
    body = json.dumps({"sql": sql, "params": params or []}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {settings.d1_api_token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:400]
        raise RuntimeError(f"D1 HTTP {exc.code}: {detail}") from None
    if not payload.get("success", False):
        raise RuntimeError(f"D1 error: {json.dumps(payload.get('errors'))[:400]}")
    return payload


def publish_catalog(companies: list, batch_size: int = 100) -> dict:
    """Upsert every company into the D1 mirror. Returns a summary.

    Rows are sent in batches because D1 caps statement size and bound
    parameters; one statement per company would be thousands of round trips.
    Companies without a SEDAR number are skipped -- the number is the key the
    downstream join is built on, so a NULL there is not a usable row.
    """
    table = settings.d1_table
    _query(_CREATE.format(table=table))

    now = datetime.now(timezone.utc).isoformat()
    rows = [c for c in companies if (c.number or "").strip()]
    skipped = len(companies) - len(rows)
    sent = 0

    for start in range(0, len(rows), batch_size):
        chunk = rows[start : start + batch_size]
        placeholders = ", ".join(["(?, ?, ?, ?, ?, ?, ?)"] * len(chunk))
        params: list = []
        for c in chunk:
            params.extend(
                [
                    c.number,
                    c.name,
                    c.jurisdiction,
                    c.type,
                    c.in_default,
                    c.cease_trade_order,
                    now,
                ]
            )
        _query(_UPSERT.format(table=table, values=placeholders), params)
        sent += len(chunk)

    return {"published": sent, "skipped_no_number": skipped, "table": table,
            "updated_at": now}
