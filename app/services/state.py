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
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Update, and_, func, or_, update

from app.models import Document

IN_FLIGHT = ("queued", "processing")             # Document.status values a worker is (supposedly) working on
INSIGHTS_IN_FLIGHT = ("pending", "processing")   # the same for Document.insights_status

# The state machine as data (source -> targets). Documentation that cannot drift: tests/ingestion/
# test_state_transitions.py runs every *_stmt below from every state and requires exactly these transitions.
TRANSITIONS: dict[str, set[str]] = {
    "queued": {"processing", "failed", "queued"},
    "processing": {"processing", "ready", "failed", "queued"},
    "ready": {"queued"},
    "failed": {"queued"},
}
INSIGHTS_TRANSITIONS: dict[str, set[str]] = {
    "pending": {"processing", "failed", "pending"},   # pending -> pending: a new run while still pending (requeue)
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


def reprocess_stmt(document_id: uuid.UUID, new_run_id: uuid.UUID, *, scope: str, stale_minutes: int) -> Update:
    """requeue_stmt with the reprocess preconditions inside the WHERE clause, so check-and-requeue is ONE atomic
    statement. Zero rows back = another reprocess got there first (the caller answers 409).

    Why not just check in Python first: two near-simultaneous requests (two tabs, "Reprocess" then "Regenerate
    insights") both pass a read-then-write check, and the second UPDATE would overwrite current_run_id with a run
    whose task can never claim the document (e.g. an insights run on a document that is now 'queued'), leaving it
    stuck with no runnable task. Postgres re-evaluates this WHERE on the newest row version after waiting for the
    first writer's lock, so the loser matches zero rows instead.

      scope=all       status is ready/failed, or queued/processing with no progress for `stale_minutes`
      scope=insights  status is ready, and insights are ready/failed or stuck pending/processing (stale)
    """
    stale = Document.updated_at < func.now() - timedelta(minutes=stale_minutes)
    if scope == "all":
        allowed = or_(Document.status.in_(["ready", "failed"]), and_(Document.status.in_(IN_FLIGHT), stale))
    else:
        allowed = and_(Document.status == "ready",
                       or_(Document.insights_status.in_(["ready", "failed"]),
                           and_(Document.insights_status.in_(INSIGHTS_IN_FLIGHT), stale)))
    return requeue_stmt(document_id, new_run_id, scope=scope).where(allowed)


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


def _idle(updated_at: datetime, now: datetime, stale_minutes: int) -> bool:
    return (now - updated_at).total_seconds() > stale_minutes * 60


def is_stale(status: str, updated_at: datetime, now: datetime, stale_minutes: int) -> bool:
    return status in IN_FLIGHT and _idle(updated_at, now, stale_minutes)


def is_insights_stale(insights_status: str, updated_at: datetime, now: datetime, stale_minutes: int) -> bool:
    """Insights that say pending/processing but have made no progress for `stale_minutes`: the task was lost (its
    worker was killed past the hard time limit, or the broker lost the message without redelivering it). A lock wait
    that gives up and a worker that dies between 'ready' and the insights publish no longer end here: the first fails
    insights as retryable, and the redelivered ingest publishes them again (worker/attempt.py). Same clock as
    is_stale: insights only run once status=ready, and every insights write (claim, ready, failed) sets updated_at,
    while a healthy attempt is cut off by the 9-minute soft limit long before 15 minutes pass without a write."""
    return insights_status in INSIGHTS_IN_FLIGHT and _idle(updated_at, now, stale_minutes)
