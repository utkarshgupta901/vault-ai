import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import BigInteger, ForeignKey, Numeric, SmallInteger, Text, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class ProcessingRun(Base):
    """One row per pipeline *attempt* (a retried task = several rows with the same run_id).
    Source of truth for the processing-metrics API: durations per stage, retries, failures by code."""

    __tablename__ = "processing_runs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    document_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("documents.id", ondelete="SET NULL"))
    owner_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    task: Mapped[str] = mapped_column(Text)             # ingest | insights
    attempt: Mapped[int] = mapped_column(SmallInteger)
    celery_task_id: Mapped[str | None] = mapped_column(Text)
    request_id: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)           # running | succeeded | failed | retrying
    stage_ms: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    error_code: Mapped[str | None] = mapped_column(Text)
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(server_default=text("now()"))
    finished_at: Mapped[datetime | None]
    duration_ms: Mapped[int | None]


class AIUsage(Base):
    """Ledger of every model call: tokens, cost, latency. No foreign keys on purpose — the cost history must
    survive document/session deletion. Powers cost tracking and the AI-usage metrics API."""

    __tablename__ = "ai_usage"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    owner_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    operation: Mapped[str] = mapped_column(Text)
    model: Mapped[str] = mapped_column(Text)
    prompt_version: Mapped[str | None] = mapped_column(Text)
    input_tokens: Mapped[int] = mapped_column(server_default=text("0"))
    cached_input_tokens: Mapped[int] = mapped_column(server_default=text("0"))
    output_tokens: Mapped[int] = mapped_column(server_default=text("0"))
    reasoning_tokens: Mapped[int] = mapped_column(server_default=text("0"))
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), server_default=text("0"))
    latency_ms: Mapped[int | None]
    success: Mapped[bool] = mapped_column(server_default=text("true"))
    error_code: Mapped[str | None] = mapped_column(Text)
    cache_hit: Mapped[bool] = mapped_column(server_default=text("false"))
    estimated: Mapped[bool] = mapped_column(server_default=text("false"))
    document_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    message_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    request_id: Mapped[str | None] = mapped_column(Text)
    task_id: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))


class RequestLog(Base):
    """HTTP request telemetry for the system-performance metrics API (latency percentiles, error rates)."""

    __tablename__ = "request_logs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    request_id: Mapped[str | None] = mapped_column(Text)
    owner_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    method: Mapped[str] = mapped_column(Text)
    route: Mapped[str] = mapped_column(Text)
    status_code: Mapped[int] = mapped_column(SmallInteger)
    ttfb_ms: Mapped[int | None]
    total_ms: Mapped[int | None]
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))
