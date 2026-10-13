"""Fixture-row helpers for the metrics tests.

Rows are inserted with plain SQL (not through the pipeline or chat code) so each test controls every value the
metrics read and can state the exact expected numbers."""

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.middleware import request_log_buffer


def ago(**delta: float) -> datetime:
    return datetime.now(UTC) - timedelta(**delta)


@pytest.fixture
async def quiet_request_logs(db: AsyncSession) -> None:
    """Start from an empty request_logs table. Rows buffered by earlier tests are dropped, and flush() waits for any
    in-flight background insert, so a late flush can't land in this test's counts."""
    request_log_buffer.discard_pending()
    await request_log_buffer.flush()
    await db.execute(text("TRUNCATE request_logs RESTART IDENTITY"))
    await db.commit()


async def add_document(db: AsyncSession, owner_id: uuid.UUID, **cols: Any) -> uuid.UUID:
    doc_id = uuid.uuid4()
    created = cols.pop("created_at", datetime.now(UTC))
    row = {
        "id": doc_id, "owner_id": owner_id, "filename": f"{doc_id.hex[:8]}.txt", "mime_type": "text/plain",
        "size_bytes": 100, "sha256": doc_id.hex * 2, "storage_key": f"uploads/{owner_id}/{doc_id}.txt",
        "status": "ready", "insights_status": "ready", "page_count": None, "chunk_count": None, "token_count": None,
        "upload_count": 1, "category": None, "tags": [], "created_at": created, "updated_at": created,
        "ready_at": None,
    } | cols
    await db.execute(text("""
        INSERT INTO documents (id, owner_id, filename, mime_type, size_bytes, sha256, storage_key, status,
                               insights_status, page_count, chunk_count, token_count, upload_count, category, tags,
                               created_at, updated_at, ready_at)
        VALUES (:id, :owner_id, :filename, :mime_type, :size_bytes, :sha256, :storage_key, :status,
                :insights_status, :page_count, :chunk_count, :token_count, :upload_count, :category, :tags,
                :created_at, :updated_at, :ready_at)"""), row)
    return doc_id


async def add_run(db: AsyncSession, owner_id: uuid.UUID, **cols: Any) -> None:
    row = {
        "run_id": uuid.uuid4(), "document_id": None, "owner_id": owner_id, "task": "ingest", "attempt": 1,
        "status": "succeeded", "stage_ms": {}, "error_code": None, "error_message": None,
        "started_at": ago(minutes=5), "finished_at": None, "duration_ms": None,
    } | cols
    row["stage_ms"] = json.dumps(row["stage_ms"])
    await db.execute(text("""
        INSERT INTO processing_runs (run_id, document_id, owner_id, task, attempt, status, stage_ms, error_code,
                                     error_message, started_at, finished_at, duration_ms)
        VALUES (:run_id, :document_id, :owner_id, :task, :attempt, :status, CAST(:stage_ms AS jsonb), :error_code,
                :error_message, :started_at, :finished_at, :duration_ms)"""), row)


async def add_usage(db: AsyncSession, owner_id: uuid.UUID, operation: str, **cols: Any) -> None:
    row = {
        "owner_id": owner_id, "operation": operation, "model": "gpt-6-luna", "input_tokens": 0,
        "cached_input_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0, "cost_usd": 0, "latency_ms": None,
        "success": True, "cache_hit": False, "estimated": False, "document_id": None,
        "created_at": ago(seconds=1),
    } | cols
    await db.execute(text("""
        INSERT INTO ai_usage (owner_id, operation, model, input_tokens, cached_input_tokens, output_tokens,
                              reasoning_tokens, cost_usd, latency_ms, success, cache_hit, estimated, document_id,
                              created_at)
        VALUES (:owner_id, :operation, :model, :input_tokens, :cached_input_tokens, :output_tokens,
                :reasoning_tokens, :cost_usd, :latency_ms, :success, :cache_hit, :estimated, :document_id,
                :created_at)"""), row)


async def add_session(db: AsyncSession, owner_id: uuid.UUID) -> uuid.UUID:
    return await db.scalar(text("INSERT INTO chat_sessions (owner_id) VALUES (:o) RETURNING id"), {"o": owner_id})


async def add_message(db: AsyncSession, session_id: uuid.UUID, **cols: Any) -> None:
    row = {
        "session_id": session_id, "role": "assistant", "content": "answer", "status": "complete",
        "answer_type": None, "confidence": None, "signals": None, "citations": [], "warnings": [],
        "ttft_ms": None, "latency_ms": None, "timings": None, "created_at": ago(minutes=1),
    } | cols
    for key in ("signals", "citations", "warnings", "timings"):
        row[key] = None if row[key] is None else json.dumps(row[key])
    await db.execute(text("""
        INSERT INTO chat_messages (session_id, role, content, status, answer_type, confidence, signals, citations,
                                   warnings, ttft_ms, latency_ms, timings, created_at)
        VALUES (:session_id, :role, :content, :status, :answer_type, :confidence, CAST(:signals AS jsonb),
                CAST(:citations AS jsonb), CAST(:warnings AS jsonb), :ttft_ms, :latency_ms, CAST(:timings AS jsonb),
                :created_at)"""), row)


async def add_request_log(db: AsyncSession, method: str, route: str, status_code: int, ttfb_ms: int,
                          created_at: datetime | None = None) -> None:
    await db.execute(text("""
        INSERT INTO request_logs (method, route, status_code, ttfb_ms, total_ms, created_at)
        VALUES (:method, :route, :status_code, :ttfb_ms, :ttfb_ms, :created_at)"""),
        {"method": method, "route": route, "status_code": status_code, "ttfb_ms": ttfb_ms,
         "created_at": created_at or ago(seconds=1)})
