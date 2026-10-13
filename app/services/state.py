"""Document state machine — the ONLY place that builds status-changing UPDATEs.

Document.status:          queued -> processing -> ready | failed ; ready|failed -> queued (reprocess)
Document.insights_status: pending -> processing -> ready | failed (runs after status=ready)

Two rules make Celery's at-least-once delivery safe:
  1. Fencing: every worker write includes `WHERE current_run_id = :run_id`. Reprocess/re-upload issues a new run_id,
     so a stale or duplicate task's writes match zero rows and become no-ops.
  2. Every write sets updated_at, which powers the `stale` flag (no progress for STALE_PROCESSING_MIN minutes).
Each helper returns a statement with RETURNING id: zero rows back = "superseded / deleted, stop quietly".
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Update, func, update

from app.models import Document

TRANSITIONS: dict[str, set[str]] = {
    "queued": {"processing", "failed", "queued"},
    "processing": {"processing", "ready", "failed", "queued"},
    "ready": {"queued"},
    "failed": {"queued"},
}
INSIGHTS_TRANSITIONS: dict[str, set[str]] = {
    "pending": {"processing", "failed"},
    "processing": {"processing", "ready", "failed", "pending"},
    "ready": {"pending"},
    "failed": {"pending"},
}


def _fenced(document_id: uuid.UUID, run_id: uuid.UUID) -> Update:
    return update(Document).where(Document.id == document_id, Document.current_run_id == run_id)


def claim_ingest_stmt(document_id: uuid.UUID, run_id: uuid.UUID) -> Update:
    return (
        _fenced(document_id, run_id)
        .where(Document.status.in_(["queued", "processing"]))
        .values(status="processing", stage="extracting", progress_pct=0, error_code=None, error_message=None,
                error_retryable=None, updated_at=func.now())
        .returning(Document.id)
    )


def progress_stmt(document_id: uuid.UUID, run_id: uuid.UUID, stage: str, pct: int, **fields: Any) -> Update:
    return (
        _fenced(document_id, run_id)
        .where(Document.status == "processing")
        .values(stage=stage, progress_pct=max(0, min(100, pct)), updated_at=func.now(), **fields)
        .returning(Document.id)
    )


def ready_stmt(document_id: uuid.UUID, run_id: uuid.UUID, **fields: Any) -> Update:
    return (
        _fenced(document_id, run_id)
        .where(Document.status == "processing")
        .values(status="ready", stage=None, progress_pct=100, ready_at=func.now(), updated_at=func.now(),
                insights_status="pending", insights_error_code=None, **fields)
        .returning(Document.id)
    )


def fail_stmt(document_id: uuid.UUID, run_id: uuid.UUID, code: str, message: str, retryable: bool) -> Update:
    return (
        _fenced(document_id, run_id)
        .where(Document.status.in_(["queued", "processing"]))
        .values(status="failed", stage=None, error_code=code, error_message=message[:2000],
                error_retryable=retryable, updated_at=func.now())
        .returning(Document.id)
    )


def requeue_stmt(document_id: uuid.UUID, new_run_id: uuid.UUID, *, scope: str = "all") -> Update:
    """Start a new run (reprocess / re-upload after a retryable failure). Not fenced: it *replaces* the run."""
    values: dict[str, Any] = {"current_run_id": new_run_id, "updated_at": func.now()}
    if scope == "all":
        values |= {"status": "queued", "stage": None, "progress_pct": 0, "error_code": None, "error_message": None,
                   "error_retryable": None, "insights_status": "pending", "insights_error_code": None}
    else:  # insights only
        values |= {"insights_status": "pending", "insights_error_code": None}
    return update(Document).where(Document.id == document_id).values(**values).returning(Document.id)


def claim_insights_stmt(document_id: uuid.UUID, run_id: uuid.UUID) -> Update:
    return (
        _fenced(document_id, run_id)
        .where(Document.status == "ready", Document.insights_status.in_(["pending", "processing"]))
        .values(insights_status="processing", insights_error_code=None, updated_at=func.now())
        .returning(Document.id)
    )


def insights_ready_stmt(document_id: uuid.UUID, run_id: uuid.UUID, **fields: Any) -> Update:
    return (
        _fenced(document_id, run_id)
        .where(Document.insights_status == "processing")
        .values(insights_status="ready", updated_at=func.now(), **fields)
        .returning(Document.id)
    )


def insights_fail_stmt(document_id: uuid.UUID, run_id: uuid.UUID, code: str) -> Update:
    return (
        _fenced(document_id, run_id)
        .where(Document.insights_status.in_(["pending", "processing"]))
        .values(insights_status="failed", insights_error_code=code, updated_at=func.now())
        .returning(Document.id)
    )


def is_stale(status: str, updated_at: datetime, now: datetime, stale_minutes: int) -> bool:
    return status in ("queued", "processing") and (now - updated_at).total_seconds() > stale_minutes * 60
