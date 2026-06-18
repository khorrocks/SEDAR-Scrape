"""Database models.

Tables:
  companies    -- the enumerated SEDAR+ catalog (powers local autocomplete).
  jobs         -- the serial download/enumeration queue.
  documents    -- per-company index of downloaded documents (+ which zip batch).

A "saved" company is just companies.saved = True, so the Saved view and the
autocomplete catalog share one table.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def _now() -> datetime:
    return datetime.now(timezone.utc)


# Job lifecycle
JOB_QUEUED = "queued"
JOB_RUNNING = "running"
JOB_DONE = "done"
JOB_FAILED = "failed"
JOB_CANCELLED = "cancelled"

# Job kinds
KIND_DOWNLOAD = "download_company"   # full download, all docs in batches of 30
KIND_RECHECK = "recheck_company"     # cron/manual: fetch only new docs
KIND_ENUMERATE = "enumerate_catalog" # populate the companies catalog
KIND_PROBE = "probe"                 # debug: load URLs and report what loaded


class Company(Base):
    __tablename__ = "companies"
    __table_args__ = (
        UniqueConstraint("number", name="uq_company_number"),
        Index("ix_company_name", "name"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # SEDAR+ issuer number, e.g. "000003771" -- the stable key from enumeration.
    number: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(512))
    jurisdiction: Mapped[str | None] = mapped_column(String(128), nullable=True)
    type: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # The opaque profile.html?id=<hash> URL, captured via the "Generate URL"
    # action when we first download. May be null if we drive by number instead.
    profile_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    profile_url: Mapped[str | None] = mapped_column(Text, nullable=True)

    saved: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    total_documents: Mapped[int] = mapped_column(Integer, default=0)
    last_download_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    documents: Mapped[list["Document"]] = relationship(
        back_populates="company", cascade="all, delete-orphan"
    )
    jobs: Mapped[list["Job"]] = relationship(
        back_populates="company", cascade="all, delete-orphan"
    )


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (Index("ix_job_status_created", "status", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), default=KIND_DOWNLOAD)
    company_id: Mapped[int | None] = mapped_column(
        ForeignKey("companies.id"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(16), default=JOB_QUEUED, index=True)

    # Free-form params for non-company jobs (e.g. enumerate type).
    params: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Progress, surfaced in the queue view.
    batches_done: Mapped[int] = mapped_column(Integer, default=0)
    documents_done: Mapped[int] = mapped_column(Integer, default=0)
    total_documents: Mapped[int] = mapped_column(Integer, default=0)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    company: Mapped["Company"] = relationship(back_populates="jobs")


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (
        # Identity of a filing within a company: title + submitted date + size.
        UniqueConstraint(
            "company_id", "dedup_key", name="uq_document_company_dedup"
        ),
        Index("ix_document_company", "company_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"))

    title: Mapped[str] = mapped_column(Text)
    submitted: Mapped[str | None] = mapped_column(String(64), nullable=True)
    jurisdiction: Mapped[str | None] = mapped_column(String(128), nullable=True)
    file_size: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Stable identity used to skip already-downloaded docs on recheck.
    dedup_key: Mapped[str] = mapped_column(String(512))

    # Which downloaded zip batch this document arrived in (relative to data_dir).
    batch_zip: Mapped[str | None] = mapped_column(Text, nullable=True)
    downloaded_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    company: Mapped["Company"] = relationship(back_populates="documents")
