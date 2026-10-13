import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import BigInteger, ForeignKey, String, Text, text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class DocumentInsight(Base):
    """AI-generated structured understanding of a document (one row per document, upserted per run)."""

    __tablename__ = "document_insights"

    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), primary_key=True
    )
    title: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    key_points: Mapped[list[Any]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    document_type: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str | None] = mapped_column(Text)
    tags: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=text("'{}'"))
    entities: Mapped[list[Any]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    sentiment: Mapped[str | None] = mapped_column(Text)
    sentiment_rationale: Mapped[str | None] = mapped_column(Text)
    risk_flags: Mapped[list[Any]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    suggested_questions: Mapped[list[Any]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    language: Mapped[str | None] = mapped_column(Text)
    document_date: Mapped[date | None]
    injection_suspected: Mapped[bool] = mapped_column(server_default=text("false"))
    coverage: Mapped[str] = mapped_column(Text, server_default=text("'full'"))
    run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    model: Mapped[str | None] = mapped_column(Text)
    prompt_version: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(server_default=text("now()"))


class DocumentSummary(Base):
    """Customized summaries (length / tone / focus), cached by a hash of every input that affects the output."""

    __tablename__ = "document_summaries"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    document_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"))
    cache_key: Mapped[str] = mapped_column(String(64))
    length: Mapped[str] = mapped_column(Text)
    tone: Mapped[str] = mapped_column(Text)
    focus: Mapped[str | None] = mapped_column(Text)
    content: Mapped[str] = mapped_column(Text)
    model: Mapped[str] = mapped_column(Text)
    prompt_version: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))
