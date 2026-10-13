import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import BigInteger, ForeignKey, Identity, Numeric, Text, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class ChatSession(Base):
    """A conversation scoped to a set of documents (or all of the owner's documents)."""

    __tablename__ = "chat_sessions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"))
    title: Mapped[str | None] = mapped_column(Text)
    all_documents: Mapped[bool] = mapped_column(server_default=text("false"))
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))
    last_message_at: Mapped[datetime | None]


class ChatSessionDocument(Base):
    __tablename__ = "chat_session_documents"

    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chat_sessions.id", ondelete="CASCADE"), primary_key=True
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), primary_key=True
    )


class ChatMessage(Base):
    """One user question or one assistant answer, with everything needed to explain the answer later:
    the standalone query used for retrieval, the retrieved candidates with their ranks, the citations (snapshotted),
    confidence signals, model, tokens, cost and per-stage timings.

    `seq` gives a total order: several rows inserted in one transaction share the same now() timestamp."""

    __tablename__ = "chat_messages"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    seq: Mapped[int] = mapped_column(BigInteger, Identity(always=True))
    session_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("chat_sessions.id", ondelete="CASCADE"))
    role: Mapped[str] = mapped_column(Text)
    reply_to_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("chat_messages.id", ondelete="SET NULL"))
    content: Mapped[str] = mapped_column(Text, server_default=text("''"))
    status: Mapped[str] = mapped_column(Text, server_default=text("'complete'"))
    client_message_id: Mapped[str | None] = mapped_column(Text)
    standalone_query: Mapped[str | None] = mapped_column(Text)
    answer_type: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[str | None] = mapped_column(Text)
    signals: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    retrieval: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    citations: Mapped[list[Any]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    followups: Mapped[list[Any]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    warnings: Mapped[list[Any]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    model: Mapped[str | None] = mapped_column(Text)
    prompt_version: Mapped[str | None] = mapped_column(Text)
    input_tokens: Mapped[int | None]
    cached_input_tokens: Mapped[int | None]
    output_tokens: Mapped[int | None]
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    ttft_ms: Mapped[int | None]
    latency_ms: Mapped[int | None]
    timings: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))
