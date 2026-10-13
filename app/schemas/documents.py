import uuid
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.schemas.common import ApiModel

DocumentStatus = Literal["queued", "processing", "ready", "failed"]
InsightsStatus = Literal["pending", "processing", "ready", "failed"]


class DocumentError(BaseModel):
    code: str
    message: str
    retryable: bool


class DocumentOut(ApiModel):
    id: uuid.UUID
    filename: str
    title: str | None
    mime_type: str
    size_bytes: int
    sha256: str
    status: DocumentStatus
    stage: str | None
    progress_pct: int
    stale: bool = False
    insights_status: InsightsStatus
    insights_error_code: str | None = None
    page_count: int | None
    chunk_count: int | None
    token_count: int | None
    category: str | None
    tags: list[str]
    document_date: date | None
    version: int
    upload_count: int
    warnings: list[Any]
    error: DocumentError | None = None
    created_at: datetime
    updated_at: datetime
    ready_at: datetime | None


class UploadLinks(BaseModel):
    self: str
    events: str
    insights: str


class UploadResponse(BaseModel):
    document: DocumentOut
    duplicate: bool = False
    requeued: bool = False
    links: UploadLinks


class DocumentStatusEvent(BaseModel):
    """Payload of `event: status` on GET /documents/{id}/events."""

    status: DocumentStatus
    stage: str | None
    progress_pct: int
    insights_status: InsightsStatus
    error: DocumentError | None = None
    stale: bool = False


class PageOut(BaseModel):
    document_id: uuid.UUID
    page_no: int
    label: str
    heading_path: str | None
    text: str


class ReprocessRequest(BaseModel):
    scope: Literal["all", "insights"] = "all"


class ReprocessResponse(BaseModel):
    document: DocumentOut
    run_id: uuid.UUID


class SummaryRequest(BaseModel):
    length: Literal["short", "medium", "detailed"] = "medium"
    tone: Literal["neutral", "executive", "technical", "simple"] = "neutral"
    focus: str | None = Field(default=None, max_length=200, description="Optional focus, e.g. 'termination and liability'")


class SummaryOut(BaseModel):
    document_id: uuid.UUID
    length: str
    tone: str
    focus: str | None
    content: str
    cached: bool
    model: str
    prompt_version: str
