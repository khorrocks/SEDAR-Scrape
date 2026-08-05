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

# sedar_number is the primary key (SEDAR's own identifier), but folder_slug is
# the column that actually matters downstream: the existing `files` table keys
# every row by "<exchange>-<ticker>" in files.company, so the slug is what joins
# a filing back to its issuer. Publishing the number alone would have produced a
# table nothing could join to.
_CREATE = """
CREATE TABLE IF NOT EXISTS {table} (
  sedar_number      TEXT PRIMARY KEY,
  name              TEXT NOT NULL,
  folder_slug       TEXT,
  exchange          TEXT,
  ticker            TEXT,
  jurisdiction      TEXT,
  profile_type      TEXT,
  in_default        TEXT,
  cease_trade_order TEXT,
  saved             INTEGER NOT NULL DEFAULT 0,
  total_documents   INTEGER,
  reported_total    INTEGER,
  is_complete       INTEGER,
  updated_at        TEXT
)
"""

_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_{table}_slug ON {table}(folder_slug)",
    "CREATE INDEX IF NOT EXISTS idx_{table}_name ON {table}(name)",
)

_COLUMNS = (
    "sedar_number", "name", "folder_slug", "exchange", "ticker", "jurisdiction",
    "profile_type", "in_default", "cease_trade_order", "saved",
    "total_documents", "reported_total", "is_complete", "updated_at",
)

_UPSERT = """
INSERT INTO {table} ({columns})
VALUES {values}
ON CONFLICT(sedar_number) DO UPDATE SET
{assignments}
"""


def _lit(value) -> str:
    """Render a Python value as a SQLite literal.

    Bulk upserts here are built as literal SQL rather than bound parameters
    because D1 caps a statement at 100 bound variables -- with 14 columns that
    is 7 rows per round trip, or ~765 requests for the catalog. Inlining gets it
    to ~54.

    Safe because SQLite string literals have exactly one escape: a single quote
    is doubled. There are no backslash escapes to worry about, and every value
    is normalised to None/bool/int/str first, so nothing reaches the SQL as an
    unquoted blob.
    """
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


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
    for stmt in _INDEXES:
        _query(stmt.format(table=table))

    now = datetime.now(timezone.utc).isoformat()
    rows = [c for c in companies if (c.number or "").strip()]
    skipped = len(companies) - len(rows)

    columns = ", ".join(_COLUMNS)
    # Every column except the conflict key is overwritten from the incoming row.
    assignments = ",\n".join(
        f"  {col} = excluded.{col}" for col in _COLUMNS if col != "sedar_number"
    )
    sent = 0
    with_slug = 0

    for start in range(0, len(rows), batch_size):
        chunk = rows[start : start + batch_size]
        values = []
        for c in chunk:
            slug = c.folder_slug or None
            if slug:
                with_slug += 1
            values.append(
                "("
                + ", ".join(
                    _lit(v)
                    for v in (
                        c.number,
                        c.name,
                        slug,
                        c.exchange,
                        c.ticker,
                        c.jurisdiction,
                        c.type,
                        c.in_default,
                        c.cease_trade_order,
                        bool(c.saved),
                        c.total_documents,
                        c.reported_total,
                        bool(c.is_complete),
                        now,
                    )
                )
                + ")"
            )
        _query(
            _UPSERT.format(
                table=table,
                columns=columns,
                values=", ".join(values),
                assignments=assignments,
            )
        )
        sent += len(chunk)

    return {"published": sent, "skipped_no_number": skipped, "with_slug": with_slug,
            "table": table, "updated_at": now}
