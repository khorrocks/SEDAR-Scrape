"""SQLAlchemy engine/session setup. Sync engine on purpose: the worker drives
Selenium (blocking) and the web endpoints are light, so a plain sync session is
simpler and avoids mixing async with the blocking browser code."""

from __future__ import annotations

import re
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import settings


class Base(DeclarativeBase):
    pass


_url = settings.resolved_database_url
_connect_args = {"check_same_thread": False} if _url.startswith("sqlite") else {}
engine = create_engine(_url, echo=False, future=True, connect_args=_connect_args)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def init_db() -> None:
    # Import models so they register on Base.metadata before create_all.
    from . import models  # noqa: F401

    Base.metadata.create_all(engine)
    _migrate()


# Columns added after the first release. create_all() never ALTERs existing
# tables, so we add any missing ones here. Kept tiny/idempotent on purpose --
# ADD COLUMN (nullable, no default) is safe on both SQLite and Postgres.
_ADDED_COLUMNS = {
    "companies": {
        "exchange": "VARCHAR(16)",
        "ticker": "VARCHAR(32)",
        "reported_total": "INTEGER DEFAULT 0",
        "is_complete": "BOOLEAN DEFAULT 0",
        "coverage_checked_at": "TIMESTAMP",
        "paused": "BOOLEAN DEFAULT 0",
        "in_default": "VARCHAR(32)",
        "cease_trade_order": "VARCHAR(32)",
    },
    "jobs": {
        "blocked": "BOOLEAN DEFAULT 0",
        "pause_requested": "BOOLEAN DEFAULT 0",
    },
    "documents": {
        "archive_member": "TEXT",
        "content_sha256": "VARCHAR(64)",
        "batch_id": "INTEGER",
    },
}


def _relax_company_number(insp) -> None:
    """Make companies.number nullable, converting existing "" to NULL.

    A CSV import creates issuers whose SEDAR number is not known yet. The old
    schema declared the column NOT NULL, so those rows were stored as "" -- and
    UNIQUE(number) treats every "" as the same value, so the second numberless
    issuer failed the whole import. NULLs are distinct under UNIQUE on both
    SQLite and Postgres, so nullable is what the column always should have been.

    SQLite cannot ALTER a column, so the table is rebuilt. Two things this must
    not assume:

      * that DDL rolls back -- pysqlite quietly commits CREATE/ALTER/DROP even
        inside a transaction, so a failure mid-way cannot be undone. The order
        below is therefore build-verify-swap: the original table is only dropped
        after the copy has been made AND its row count checked, so any failure
        leaves the original intact and merely strands a scratch table;
      * that the model's CREATE TABLE can hold the old rows. It cannot -- older
        rows predate columns that are now NOT NULL (paused, saved), so copying
        into a model-generated table fails on them. Reusing the live table's own
        definition with just the one NOT NULL removed keeps the copy total.
    """
    if "companies" not in set(insp.get_table_names()):
        return
    cols = {c["name"]: c for c in insp.get_columns("companies")}
    if "number" not in cols or cols["number"].get("nullable", True):
        return  # already nullable, nothing to do

    if not engine.url.get_backend_name().startswith("sqlite"):
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE companies ALTER COLUMN number DROP NOT NULL"))
            conn.execute(text("UPDATE companies SET number = NULL WHERE number = ''"))
        print("[db] migrated: companies.number is now nullable", flush=True)
        return

    with engine.begin() as conn:
        old_sql = conn.execute(
            text("SELECT sql FROM sqlite_master WHERE type='table' AND name='companies'")
        ).scalar()
        index_sqls = conn.execute(
            text(
                "SELECT sql FROM sqlite_master WHERE type='index' "
                "AND tbl_name='companies' AND sql IS NOT NULL"
            )
        ).scalars().all()
    if not old_sql:
        return

    # Drop NOT NULL from the number column only, leaving every other column
    # definition byte-for-byte as it is.
    new_sql, n = re.subn(
        r'("?number"?\s+[A-Za-z0-9_()]+)\s+NOT\s+NULL',
        r"\1", old_sql, count=1, flags=re.IGNORECASE,
    )
    if not n:
        print("[db] companies.number: could not locate NOT NULL, leaving as is", flush=True)
        return
    new_sql = re.sub(
        r'^(CREATE\s+TABLE\s+)"?companies"?', r"\1companies_rebuild",
        new_sql, count=1, flags=re.IGNORECASE,
    )
    shared = ", ".join(f'"{c}"' for c in cols)
    select_list = ", ".join(
        "NULLIF(number, '')" if c == "number" else f'"{c}"' for c in cols
    )

    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS companies_rebuild"))
        conn.execute(text(new_sql))
        conn.execute(
            text(f"INSERT INTO companies_rebuild ({shared}) SELECT {select_list} FROM companies")
        )
        before = conn.execute(text("SELECT COUNT(*) FROM companies")).scalar()
        after = conn.execute(text("SELECT COUNT(*) FROM companies_rebuild")).scalar()
        if before != after:
            conn.execute(text("DROP TABLE IF EXISTS companies_rebuild"))
            raise RuntimeError(
                f"companies rebuild copied {after} of {before} rows; original left untouched"
            )
        # Only now is it safe to swap. Dropping the original also drops its
        # indexes, freeing their names for the recreate below.
        conn.execute(text("DROP TABLE companies"))
        conn.execute(text("ALTER TABLE companies_rebuild RENAME TO companies"))
        for sql in index_sqls:
            conn.execute(text(sql))
    print(f"[db] migrated: companies.number is now nullable ({after} rows)", flush=True)


def _migrate() -> None:
    insp = inspect(engine)
    existing_tables = set(insp.get_table_names())
    for table, columns in _ADDED_COLUMNS.items():
        if table not in existing_tables:
            continue
        have = {c["name"] for c in insp.get_columns(table)}
        with engine.begin() as conn:
            for name, ddl in columns.items():
                if name not in have:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))
    # Re-inspect: the rebuild below copies whatever columns now exist. Guarded
    # because the web process and the worker both call init_db() at boot -- if
    # they race, the loser must log and move on rather than crash the container.
    # The whole rebuild is one transaction, so a loser leaves nothing behind.
    try:
        _relax_company_number(inspect(engine))
    except Exception as exc:
        print(f"[db] companies.number migration skipped: {exc}", flush=True)


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional session for the worker and scripts."""
    s = SessionLocal()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def get_db() -> Iterator[Session]:
    """FastAPI dependency."""
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()
