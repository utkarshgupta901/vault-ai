"""state.TRANSITIONS / INSIGHTS_TRANSITIONS are the documented state machine. This keeps them true: every
status-changing statement in state.py is run against a real row in every (status, insights_status) combination, and
the transitions the statements actually make must be exactly the ones the maps list. A new *_stmt builder, or a
WHERE clause that lets a new source state through, fails here until the maps say so."""

import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import func, insert, select, update

from app.core.db import sync_session
from app.models import Document
from app.services import state

STATUSES = ("queued", "processing", "ready", "failed")
INSIGHTS_STATUSES = ("pending", "processing", "ready", "failed")

# Every builder, called the way the code calls it. reprocess uses stale_minutes=0 so its "stuck" branch is reachable.
BUILDERS: dict[str, Any] = {
    "claim_ingest_stmt": lambda d, run, new: state.claim_ingest_stmt(d, run),
    "progress_stmt": lambda d, run, new: state.progress_stmt(d, run, "embedding", 50),
    "ready_stmt": lambda d, run, new: state.ready_stmt(d, run),
    "fail_stmt": lambda d, run, new: state.fail_stmt(d, run, "X", "x", retryable=True),
    "requeue_stmt": lambda d, run, new: state.requeue_stmt(d, new, scope="all"),
    "requeue_stmt/insights": lambda d, run, new: state.requeue_stmt(d, new, scope="insights"),
    "reprocess_stmt": lambda d, run, new: state.reprocess_stmt(d, new, scope="all", stale_minutes=0),
    "reprocess_stmt/insights": lambda d, run, new: state.reprocess_stmt(d, new, scope="insights", stale_minutes=0),
    "claim_insights_stmt": lambda d, run, new: state.claim_insights_stmt(d, run),
    "insights_ready_stmt": lambda d, run, new: state.insights_ready_stmt(d, run),
    "insights_fail_stmt": lambda d, run, new: state.insights_fail_stmt(d, run, "X"),
}


def _edges(transitions: dict[str, set[str]]) -> set[tuple[str, str]]:
    return {(src, dst) for src, targets in transitions.items() for dst in targets}


def _new_document(owner_id: uuid.UUID) -> uuid.UUID:
    doc_id = uuid.uuid4()
    with sync_session() as s:
        s.execute(insert(Document).values(id=doc_id, owner_id=owner_id, filename="t.txt", mime_type="text/plain",
                                          size_bytes=1, sha256=doc_id.hex * 2, storage_key="uploads/t.txt"))
    return doc_id


def _observed_transitions(doc_id: uuid.UUID) -> tuple[set[tuple[str, str]], set[tuple[str, str]]]:
    """(status edges, insights_status edges) the statements make. A column counts only when the statement matched a
    row AND writes that column (a self-loop like processing -> processing is a real, documented transition)."""
    status_edges: set[tuple[str, str]] = set()
    insights_edges: set[tuple[str, str]] = set()
    run = uuid.uuid4()
    for build in BUILDERS.values():
        for status in STATUSES:
            for insights in INSIGHTS_STATUSES:
                stmt = build(doc_id, run, uuid.uuid4())
                written = stmt.compile().params            # SET columns appear under their own name
                with sync_session() as s:
                    s.execute(update(Document).where(Document.id == doc_id).values(
                        status=status, insights_status=insights, current_run_id=run,
                        updated_at=func.now() - timedelta(hours=1)))
                    matched = s.execute(stmt).first() is not None
                    new_status, new_insights = s.execute(
                        select(Document.status, Document.insights_status).where(Document.id == doc_id)).one()
                if matched and "status" in written:
                    status_edges.add((status, new_status))
                if matched and "insights_status" in written:
                    insights_edges.add((insights, new_insights))
    return status_edges, insights_edges


def test_every_status_changing_builder_is_covered() -> None:
    assert {name.split("/")[0] for name in BUILDERS} == {n for n in dir(state) if n.endswith("_stmt")}


async def test_statements_make_exactly_the_documented_transitions(demo_user_id: uuid.UUID) -> None:
    status_edges, insights_edges = _observed_transitions(_new_document(demo_user_id))
    assert status_edges == _edges(state.TRANSITIONS)
    assert insights_edges == _edges(state.INSIGHTS_TRANSITIONS)
