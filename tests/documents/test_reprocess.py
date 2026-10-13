"""POST /documents/{id}/reprocess: when it is allowed (including stuck runs), what a queue outage leaves behind, the
race between two requests, and the cost guards. Eager Celery runs the queued tasks inline."""

import uuid

import pytest
from httpx import AsyncClient

from app.services import documents as svc
from app.worker.dispatch import QueueUnavailable
from app.worker.tasks import ingest_document
from tests.conftest import DEMO, upload
from tests.ingestion.helpers import get_doc, requeue
from tests.insights.helpers import sql

TEXT = b"Larkspur Analytics handbook. Employees receive 18 PTO days per year. Incident LRK-4471."


async def _ready_doc(client: AsyncClient, data: bytes = TEXT) -> str:
    r = await upload(client, "handbook.txt", data)
    assert r.status_code == 202, r.text
    doc_id = r.json()["document"]["id"]
    assert get_doc(doc_id).insights_status == "ready"
    return doc_id


async def _reprocess(client: AsyncClient, doc_id: str, scope: str):
    return await client.post(f"/api/v1/documents/{doc_id}/reprocess", headers=DEMO, json={"scope": scope})


async def _set(doc_id: str, assignments: str) -> None:
    await sql(f"UPDATE documents SET {assignments} WHERE id = :id", id=doc_id)


# --- stuck runs ----------------------------------------------------------------------------------------------------
async def test_stuck_insights_can_be_regenerated_once_stale(client: AsyncClient) -> None:
    # The document is 'ready' (so never "stale" itself) but its insights task was lost two hours ago.
    doc_id = await _ready_doc(client)
    await _set(doc_id, "insights_status = 'processing', updated_at = now() - interval '2 hours'")

    r = await _reprocess(client, doc_id, "insights")

    assert r.status_code == 202, r.text
    assert get_doc(doc_id).insights_status == "ready"


@pytest.mark.parametrize("insights_status", ["pending", "processing"])
async def test_insights_still_in_progress_are_not_started_twice(client: AsyncClient, insights_status: str) -> None:
    doc_id = await _ready_doc(client)
    await _set(doc_id, f"insights_status = '{insights_status}', updated_at = now()")
    r = await _reprocess(client, doc_id, "insights")
    assert r.status_code == 409 and r.json()["error"]["code"] == "ALREADY_PROCESSING"


async def test_stuck_ingest_can_be_reprocessed_only_once_stale(client: AsyncClient) -> None:
    doc_id = await _ready_doc(client)
    await _set(doc_id, "status = 'queued', updated_at = now()")
    assert (await _reprocess(client, doc_id, "all")).status_code == 409

    await _set(doc_id, "updated_at = now() - interval '2 hours'")
    r = await _reprocess(client, doc_id, "all")
    assert r.status_code == 202, r.text
    assert get_doc(doc_id).status == "ready"


# --- queue outage --------------------------------------------------------------------------------------------------
async def test_regenerate_with_queue_down_fails_insights_so_it_can_be_retried(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    doc_id = await _ready_doc(client)

    async def broker_down(*_args, **_kwargs) -> None:
        raise QueueUnavailable("connection refused")

    monkeypatch.setattr(svc, "aenqueue_insights", broker_down)
    r = await _reprocess(client, doc_id, "insights")
    assert r.status_code == 503 and r.json()["error"]["code"] == "QUEUE_UNAVAILABLE"
    doc = get_doc(doc_id)
    assert doc.status == "ready"                                   # chat keeps working
    assert doc.insights_status == "failed" and doc.insights_error_code == "QUEUE_UNAVAILABLE"

    monkeypatch.undo()                                             # the broker is back
    r = await _reprocess(client, doc_id, "insights")
    assert r.status_code == 202, r.text
    assert get_doc(doc_id).insights_status == "ready"


# --- two requests racing -------------------------------------------------------------------------------------------
async def test_reprocess_that_loses_a_race_is_409_and_strands_nothing(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """'Regenerate insights' reads status=ready, then another tab's 'Reprocess' commits before our UPDATE. Before,
    the unconditional UPDATE gave the queued document an insights run_id: no task could ever claim it."""
    doc_id = await _ready_doc(client)
    winner: dict[str, uuid.UUID] = {}
    get_owned = svc.get_owned

    async def read_then_lose_the_race(db, owner_id, document_id):
        doc = await get_owned(db, owner_id, document_id)       # our read: status 'ready'
        winner["run"] = requeue(document_id, scope="all")      # the other request commits right after it
        return doc

    monkeypatch.setattr(svc, "get_owned", read_then_lose_the_race)
    r = await _reprocess(client, doc_id, "insights")

    assert r.status_code == 409 and r.json()["error"]["code"] == "ALREADY_PROCESSING"
    doc = get_doc(doc_id)
    assert doc.status == "queued" and doc.current_run_id == winner["run"]   # the winner's run is intact...
    ingest_document.apply(kwargs={"document_id": doc_id, "run_id": str(winner["run"])})
    assert get_doc(doc_id).status == "ready"                                # ...and its task can still run


# --- cost guards ---------------------------------------------------------------------------------------------------
async def test_over_budget_user_can_neither_upload_nor_reprocess(client: AsyncClient,
                                                                 demo_user_id: uuid.UUID) -> None:
    doc_id = await _ready_doc(client)
    await sql("INSERT INTO ai_usage (owner_id, operation, model, cost_usd) VALUES (:o, 'answer', 'test', 99999)",
              o=demo_user_id)

    r = await upload(client, "new.txt", b"A brand new file that would need paid embeddings.")
    assert r.status_code == 429 and r.json()["error"]["code"] == "QUOTA_EXCEEDED"
    for scope in ("all", "insights"):
        r = await _reprocess(client, doc_id, scope)
        assert r.status_code == 429 and r.json()["error"]["code"] == "QUOTA_EXCEEDED"
    assert get_doc(doc_id).status == "ready"
