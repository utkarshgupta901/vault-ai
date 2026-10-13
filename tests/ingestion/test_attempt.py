"""Worker reliability rules (worker/attempt.py): backoff, run_id fencing, crash counter, retries, failure codes,
advisory-lock contention, and insights failures that leave the document usable.

Eager Celery note: task.retry() re-runs the task inline after the failed attempt returns and ignores the countdown,
so retry tests are fast; max_retries is lowered with monkeypatch where exhaustion is tested."""

import random
import sys
import types
import uuid

import pytest
from celery.exceptions import SoftTimeLimitExceeded
from httpx import AsyncClient

from app.ai.fake_provider import FakeProvider
from app.ai.provider import ProviderError
from app.core.errors import ErrorCode, PermanentError
from app.worker import attempt as attempt_module
from app.worker.attempt import MAX_LOCK_WAITS, advisory_lock, backoff
from app.worker.tasks import generate_insights, ingest_document
from tests.conftest import DEMO, upload
from tests.ingestion.helpers import (
    chunks_of,
    count,
    get_doc,
    insert_running_attempts,
    make_pdf,
    requeue,
    runs_of,
)

TEXT = b"Larkspur Analytics handbook. Employees receive 18 PTO days per year. Incident LRK-4471."


async def _ready_doc(client: AsyncClient, data: bytes = TEXT, name: str = "handbook.txt") -> str:
    r = await upload(client, name, data)
    assert r.status_code == 202, r.text
    doc_id = r.json()["document"]["id"]
    assert get_doc(doc_id).status == "ready"
    return doc_id


async def _error(client: AsyncClient, doc_id: str) -> dict | None:
    return (await client.get(f"/api/v1/documents/{doc_id}", headers=DEMO)).json()["error"]


def _run_ingest(doc_id: str, run_id: uuid.UUID | str, lock_waits: int = 0) -> None:
    ingest_document.apply(kwargs={"document_id": str(doc_id), "run_id": str(run_id), "request_id": "req_test",
                                  "lock_waits": lock_waits})


# --- backoff -------------------------------------------------------------------------------------------------------


def test_backoff_honours_retry_after_with_a_cap() -> None:
    assert backoff(0, 7) == 7
    assert backoff(5, 3.5) == 3.5
    assert backoff(0, 900) == 120


def test_backoff_is_exponential_with_equal_jitter() -> None:
    random.seed(1)
    for retries in range(10):
        base = min(120, 4 * 2 ** retries)          # 4, 8, 16, 32, 64, 120, 120 ...
        samples = [backoff(retries) for _ in range(200)]
        assert all(base / 2 <= s <= base for s in samples)   # never near zero, never above the cap
        assert max(samples) - min(samples) > base / 4        # actually jittered
    assert backoff(0, 0) <= 4 and backoff(0, None) >= 2      # Retry-After of 0 / missing -> computed backoff


# --- run_id fencing ------------------------------------------------------------------------------------------------


async def test_task_with_stale_run_id_writes_nothing(client: AsyncClient) -> None:
    doc_id = await _ready_doc(client)
    before = get_doc(doc_id)
    chunk_ids = [c.id for c in chunks_of(doc_id)]

    _run_ingest(doc_id, uuid.uuid4())
    generate_insights.apply(kwargs={"document_id": doc_id, "run_id": str(uuid.uuid4())})

    after = get_doc(doc_id)
    assert (after.status, after.updated_at, after.current_run_id) == (before.status, before.updated_at,
                                                                      before.current_run_id)
    assert [c.id for c in chunks_of(doc_id)] == chunk_ids
    assert len(runs_of(doc_id)) == 1 and len(runs_of(doc_id, "insights")) == 1   # no new attempt rows


async def test_reprocess_during_a_run_stops_the_old_run_quietly(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    doc_id = await _ready_doc(client)
    old_run = requeue(doc_id)
    newer = {}
    from app.ingestion import pipeline

    real_extract = pipeline.extract

    def extract_then_user_clicks_reprocess(path, mime):
        result = real_extract(path, mime)
        newer["run"] = requeue(doc_id)          # the API issues a new run_id while we are extracting
        return result

    monkeypatch.setattr(pipeline, "extract", extract_then_user_clicks_reprocess)
    _run_ingest(doc_id, old_run)

    doc = get_doc(doc_id)
    assert doc.status == "queued" and doc.current_run_id == newer["run"]     # the old run did not touch it
    old_attempts = [r for r in runs_of(doc_id) if r.run_id == old_run]
    assert [(r.status, r.error_code) for r in old_attempts] == [("failed", "SUPERSEDED")]


# --- crash counter -------------------------------------------------------------------------------------------------


async def test_crashed_attempts_are_closed_as_worker_lost_and_the_run_continues(client: AsyncClient) -> None:
    doc_id = await _ready_doc(client)
    run = requeue(doc_id)
    insert_running_attempts(doc_id, run, 2)

    _run_ingest(doc_id, run)

    assert get_doc(doc_id).status == "ready"
    attempts = [(r.attempt, r.status, r.error_code) for r in runs_of(doc_id) if r.run_id == run]
    assert attempts == [(1, "failed", "WORKER_LOST"), (2, "failed", "WORKER_LOST"), (3, "succeeded", None)]


async def test_third_crash_fails_the_run_as_processing_crashed(client: AsyncClient) -> None:
    doc_id = await _ready_doc(client)
    run = requeue(doc_id)
    insert_running_attempts(doc_id, run, 3)

    _run_ingest(doc_id, run)

    assert get_doc(doc_id).status == "failed"
    error = await _error(client, doc_id)
    assert error["code"] == "PROCESSING_CRASHED" and error["retryable"] is False
    attempts = [(r.status, r.error_code) for r in runs_of(doc_id) if r.run_id == run]
    assert attempts == [("failed", "WORKER_LOST")] * 3        # no 4th attempt was started


# --- retries and failure codes -------------------------------------------------------------------------------------


async def test_transient_error_is_retried_then_succeeds(client: AsyncClient, fake: FakeProvider) -> None:
    fake.fail_next.append(ProviderError("transient", "Rate limited by OpenAI.", retry_after=2, status_code=429))
    doc_id = await _ready_doc(client)
    attempts = [(r.attempt, r.status, r.error_code) for r in runs_of(doc_id)]
    assert attempts == [(1, "retrying", "TRANSIENT"), (2, "succeeded", None)]
    assert len(chunks_of(doc_id)) == 1


async def test_transient_errors_exhaust_retries_as_provider_unavailable_retryable(
    client: AsyncClient, fake: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ingest_document, "max_retries", 2)
    fake.fail_next.extend(ProviderError("transient", "OpenAI server error (503).") for _ in range(3))
    r = await upload(client, "a.txt", TEXT)
    doc_id = r.json()["document"]["id"]

    error = await _error(client, doc_id)
    assert error["code"] == "PROVIDER_UNAVAILABLE" and error["retryable"] is True
    assert [r.status for r in runs_of(doc_id)] == ["retrying", "retrying", "failed"]

    # Retryable means re-uploading the same bytes starts a new run (and the provider is healthy again).
    again = await upload(client, "a.txt", TEXT)
    assert again.status_code == 202 and again.json()["requeued"] is True
    assert get_doc(doc_id).status == "ready"


@pytest.mark.parametrize(
    ("raised", "code", "retryable"),
    [
        (SoftTimeLimitExceeded(), "PROCESSING_TIMEOUT", True),
        (ProviderError("bad_request", "OpenAI rejected the request (400)."), "PROVIDER_BAD_REQUEST", False),
        (PermanentError(ErrorCode.CORRUPT_FILE, "Broken file."), "CORRUPT_FILE", False),
        (RuntimeError("boom: internal detail"), "PROCESSING_ERROR", True),
    ],
)
async def test_failures_map_to_codes_on_get_document(
    client: AsyncClient, fake: FakeProvider, raised: Exception, code: str, retryable: bool
) -> None:
    fake.fail_next.append(raised)          # raised by the embed call inside the pipeline
    r = await upload(client, "a.txt", TEXT)
    doc_id = r.json()["document"]["id"]
    error = await _error(client, doc_id)
    assert error["code"] == code and error["retryable"] is retryable
    assert "internal detail" not in error["message"]                  # technical detail stays internal...
    (run,) = runs_of(doc_id)
    assert run.status == "failed" and run.error_code == code
    if code == "PROCESSING_ERROR":
        assert "internal detail" in run.error_message                 # ...in processing_runs and the logs


# --- advisory lock -------------------------------------------------------------------------------------------------


async def test_lock_contention_republishes_without_using_a_retry(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    doc_id = await _ready_doc(client)
    run = requeue(doc_id)
    published: list[dict] = []
    monkeypatch.setattr(ingest_document, "apply_async", lambda **kw: published.append(kw))

    with advisory_lock(f"ingest:{doc_id}") as held:        # another worker is on this document
        assert held
        _run_ingest(doc_id, run, lock_waits=3)
        _run_ingest(doc_id, run, lock_waits=MAX_LOCK_WAITS)   # waited long enough: give up quietly

    assert published == [{"kwargs": {"document_id": doc_id, "run_id": str(run), "request_id": "req_test",
                                     "lock_waits": 4}, "countdown": 15}]
    assert get_doc(doc_id).status == "queued"
    assert [r for r in runs_of(doc_id) if r.run_id == run] == []      # no attempt row, no retry consumed

    _run_ingest(doc_id, run)                                          # lock released: the run proceeds
    assert get_doc(doc_id).status == "ready"


# --- insights task -------------------------------------------------------------------------------------------------


def _fake_insights_module(run_insights) -> types.ModuleType:
    module = types.ModuleType("app.ai.insights")
    module.run_insights = run_insights
    return module


async def test_insights_failure_keeps_document_ready(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(document_id, run_id, *, task_id, request_id):
        raise ProviderError("refusal", "I can't help with that.")

    monkeypatch.setitem(sys.modules, "app.ai.insights", _fake_insights_module(refuse))
    r = await upload(client, "a.txt", TEXT)
    doc = (await client.get(f"/api/v1/documents/{r.json()['document']['id']}", headers=DEMO)).json()
    assert doc["status"] == "ready" and doc["error"] is None
    assert doc["insights_status"] == "failed" and doc["insights_error_code"] == "INSIGHTS_REFUSED"
    assert [(r.status, r.error_code) for r in runs_of(doc["id"], "insights")] == [("failed", "INSIGHTS_REFUSED")]


async def test_insights_timings_are_recorded_on_the_attempt(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = {}

    def run_insights(document_id, run_id, *, task_id, request_id):
        seen.update(document_id=document_id, run_id=run_id, request_id=request_id)
        from app.core.db import sync_session
        from app.services import state

        with sync_session() as s:
            s.execute(state.insights_ready_stmt(document_id, run_id))
        return {"load": 3, "llm": 120, "verify": 2}

    monkeypatch.setitem(sys.modules, "app.ai.insights", _fake_insights_module(run_insights))
    doc_id = await _ready_doc(client)
    assert get_doc(doc_id).insights_status == "ready"
    (run,) = runs_of(doc_id, "insights")
    assert run.status == "succeeded" and run.stage_ms == {"load": 3, "llm": 120, "verify": 2}
    assert isinstance(seen["document_id"], uuid.UUID) and seen["run_id"] == get_doc(doc_id).current_run_id


async def test_insights_crash_limit_fails_insights_only(client: AsyncClient) -> None:
    doc_id = await _ready_doc(client)
    run = requeue(doc_id, scope="insights")
    insert_running_attempts(doc_id, run, 3, task="insights")
    generate_insights.apply(kwargs={"document_id": doc_id, "run_id": str(run)})
    doc = get_doc(doc_id)
    assert doc.status == "ready"
    assert doc.insights_status == "failed" and doc.insights_error_code == "PROCESSING_CRASHED"


def test_attempt_module_has_one_message_per_user_facing_code() -> None:
    # Every code the attempt layer can put on a document has a human message (no raw exception text leaks).
    for code in ("PROVIDER_UNAVAILABLE", "PROVIDER_BAD_REQUEST", "INSIGHTS_REFUSED", "INSIGHTS_TRUNCATED",
                 "PROCESSING_TIMEOUT", "PROCESSING_CRASHED", "PROCESSING_ERROR"):
        assert attempt_module._USER_MESSAGES[code]


async def test_pdf_with_many_chunks_and_small_batches(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "EMBED_BATCH_SIZE", 2)
    doc_id = await _ready_doc(client, make_pdf(), "report.pdf")
    assert count("SELECT count(*) FROM ai_usage WHERE operation = 'embed_chunks' AND document_id = :d",
                 d=doc_id) == 2           # 3 chunks in batches of 2 -> 2 paid calls
