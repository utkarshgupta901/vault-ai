"""Publishing is bounded: a stuck broker becomes QueueUnavailable (-> 503 QUEUE_UNAVAILABLE) within seconds, never a
request that hangs forever with its document stuck in 'queued'."""

import time
import uuid

import pytest
from httpx import AsyncClient

from app.worker import dispatch
from app.worker.celery_app import celery_app
from tests.conftest import DEMO, upload


def stuck_publish(*_: object, **__: object) -> None:
    time.sleep(1.0)   # stands in for a broker that accepted the connection and never answers


async def test_stuck_publish_raises_queue_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dispatch, "PUBLISH_TIMEOUT_S", 0.2)
    monkeypatch.setattr(dispatch, "enqueue_ingest", stuck_publish)
    started = time.perf_counter()
    with pytest.raises(dispatch.QueueUnavailable, match="did not accept the task"):
        await dispatch.aenqueue_ingest(uuid.uuid4(), uuid.uuid4())
    assert time.perf_counter() - started < 0.6     # abandoned the thread instead of waiting for it


async def test_upload_with_stuck_broker_is_503_and_retryable(client: AsyncClient,
                                                            monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dispatch, "PUBLISH_TIMEOUT_S", 0.2)
    monkeypatch.setattr(dispatch, "enqueue_ingest", stuck_publish)
    r = await upload(client, "a.txt", b"Larkspur handbook: 18 PTO days per year.")
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "QUEUE_UNAVAILABLE"
    doc_id = r.json()["error"]["details"]["document_id"]
    doc = (await client.get(f"/api/v1/documents/{doc_id}", headers=DEMO)).json()
    assert doc["status"] == "failed"
    assert doc["error"]["code"] == "QUEUE_UNAVAILABLE" and doc["error"]["retryable"] is True


def test_broker_calls_have_socket_timeouts() -> None:
    """Without socket_timeout a broker that stops answering blocks a publishing thread forever."""
    options = celery_app.conf.broker_transport_options
    assert options["socket_connect_timeout"] <= 2
    assert 1 < options["socket_timeout"] <= dispatch.PUBLISH_TIMEOUT_S   # > kombu's 1 s BRPOP timeout
    assert celery_app.conf.broker_pool_limit == 10
