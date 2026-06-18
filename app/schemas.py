"""Pydantic response/request models."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class CompanyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    number: str
    name: str
    jurisdiction: str | None = None
    type: str | None = None
    saved: bool
    profile_url: str | None = None
    total_documents: int = 0
    last_download_at: datetime | None = None
    last_checked_at: datetime | None = None


class DocumentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    submitted: str | None = None
    jurisdiction: str | None = None
    file_size: str | None = None
    batch_zip: str | None = None
    downloaded_at: datetime


class JobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    kind: str
    status: str
    company_id: int | None = None
    company_name: str | None = None
    batches_done: int = 0
    documents_done: int = 0
    total_documents: int = 0
    message: str | None = None
    error: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None


class EnumerateRequest(BaseModel):
    # None enumerates every issuer type; a string filters on the row Type.
    profile_type: str | None = "Company"
    max_pages: int | None = None


class SaveRequest(BaseModel):
    # If true, queue a full download immediately after saving.
    download: bool = True


class AddCompanyRequest(BaseModel):
    # SEDAR issuer number (e.g. "000003771"). The download drives off this via
    # the "Profile name or number" lookup, so a company need not be enumerated.
    number: str
    name: str | None = None
    download: bool = True
