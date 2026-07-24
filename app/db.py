"""SQLAlchemy engine/session setup. Sync engine on purpose: the worker drives
Selenium (blocking) and the web endpoints are light, so a plain sync session is
simpler and avoids mixing async with the blocking browser code."""

from __future__ import annotations

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
    },
    "jobs": {
        "blocked": "BOOLEAN DEFAULT 0",
        "pause_requested": "BOOLEAN DEFAULT 0",
    },
}


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
