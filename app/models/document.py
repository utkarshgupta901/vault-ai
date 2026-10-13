import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import BigInteger, ForeignKey, SmallInteger, String, Text, text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class Document(Base):
    """One uploaded file and its lifecycle.

    status:          queued -> processing -> ready | failed   (ingestion: extract, chunk, embed)
    insights_status: pending -> processing -> ready | failed  (runs after `ready`; chat works without it)
    current_run_id:  fences stale/duplicate Celery deliveries — a task only writes if its run_id is still current.
    """

    __tablename__ = "documents"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"))
    filename: Mapped[str] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text)
    mime_type: Mapped[str] = mapped_column(Text)
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(String(64))
    storage_key: Mapped[str] = mapped_column(Text)
    upload_count: Mapped[int] = mapped_column(server_default=text("1"))

    status: Mapped[str] = mapped_column(Text, server_default=text("'queued'"))
    stage: Mapped[str | None] = mapped_column(Text)
    progress_pct: Mapped[int] = mapped_column(SmallInteger, server_default=text("0"))
    insights_status: Mapped[str] = mapped_column(Text, server_default=text("'pending'"))
    insights_error_code: Mapped[str | None] = mapped_column(Text)
    current_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))

    page_count: Mapped[int | None]
    chunk_count: Mapped[int | None]
    token_count: Mapped[int | None]
    warnings: Mapped[list[Any]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    error_code: Mapped[str | None] = mapped_column(Text)
    error_message: Mapped[str | None] = mapped_column(Text)
    error_retryable: Mapped[bool | None]

    category: Mapped[str | None] = mapped_column(Text)
    tags: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=text("'{}'"))
    document_date: Mapped[date | None]
    version: Mapped[int] = mapped_column(server_default=text("1"))
    previous_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="SET NULL")
    )

    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(server_default=text("now()"))
    ready_at: Mapped[datetime | None]


class DocumentPage(Base):
    """Extracted text per physical page (PDF) / section (DOCX) / pseudo-page (TXT, MD).
    Backs citation click-through: a citation says "p. 4" and the UI fetches exactly that page."""

    __tablename__ = "document_pages"

    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), primary_key=True
    )
    page_no: Mapped[int] = mapped_column(primary_key=True)
    label: Mapped[str] = mapped_column(Text)
    heading_path: Mapped[str | None] = mapped_column(Text)
    text: Mapped[str] = mapped_column(Text)
    char_count: Mapped[int]
