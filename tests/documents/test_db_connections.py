"""The document routes hold no Postgres connection while bytes move: not while the original file streams to the
client (GET /documents/{id}/file), and not while an upload is written to S3 (POST /documents). The API pool is small
(DB_POOL_SIZE + DB_MAX_OVERFLOW = 10), so ten slow downloads each sitting "idle in transaction" would starve every
tenant.

How it is measured: the S3 calls are wrapped so that, while bytes move, they record how many pool connections are
checked out, compared with the count just before the request. httpx's ASGITransport buffers the response before it
returns it, but that does not weaken the check: the body iterator still runs inside the app call, at the same point
of the request's life as under a real server (after the handler returned, before FastAPI exits the request-scoped
dependencies, which is when a `Depends(get_db)` session would finally give its connection back).
Request-log rows are dropped so the background flusher can't take a connection mid-measurement, and the ingest
publish is a no-op: the pipeline is not under test, and under load it can outlast the 5 s publish timeout."""

from typing import IO, Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_async_engine
from app.core.middleware import request_log_buffer
from app.services import documents as svc
from app.services import storage
from tests.conftest import DEMO, upload

TEXT = b"Larkspur Analytics handbook. Employees receive 18 PTO days per year."


def checked_out() -> int:
    return get_async_engine().pool.checkedout()   # read the pool each time: a lifespan dispose() replaces it


@pytest.fixture
async def quiet(monkeypatch: pytest.MonkeyPatch) -> None:
    request_log_buffer.discard_pending()
    await request_log_buffer.flush()
    monkeypatch.setattr(request_log_buffer, "add", lambda row: None)

    async def no_publish(*args: Any) -> None:
        pass

    monkeypatch.setattr(svc, "aenqueue_ingest", no_publish)


class RecordingBody:
    """Wraps the S3 StreamingBody: records the pool's checked-out count at every chunk it hands to the response."""

    def __init__(self, real: Any, seen: list[int]):
        self.real, self.seen = real, seen

    def iter_chunks(self, size: int) -> Any:
        for chunk in self.real.iter_chunks(size):
            self.seen.append(checked_out())
            yield chunk

    def close(self) -> None:
        self.real.close()


async def test_download_streams_without_holding_a_db_connection(client: AsyncClient, quiet: None,
                                                                monkeypatch: pytest.MonkeyPatch) -> None:
    doc_id = (await upload(client, "a.txt", TEXT)).json()["document"]["id"]
    seen: list[int] = []
    real_open = storage.open_stream
    monkeypatch.setattr(storage, "open_stream", lambda key: RecordingBody(real_open(key), seen))

    baseline = checked_out()
    r = await client.get(f"/api/v1/documents/{doc_id}/file", headers=DEMO)

    assert (r.status_code, r.content) == (200, TEXT)
    assert seen == [baseline]                      # one chunk, sent while no request session held a connection
    assert checked_out() == baseline


async def test_upload_writes_to_s3_without_holding_a_db_connection(client: AsyncClient, quiet: None,
                                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[int] = []
    real_put = storage.put_file

    def put_file(key: str, fileobj: IO[bytes], content_type: str) -> None:
        seen.append(checked_out())
        real_put(key, fileobj, content_type)

    monkeypatch.setattr(storage, "put_file", put_file)

    baseline = checked_out()
    r = await upload(client, "a.txt", TEXT)

    assert r.status_code == 202, r.text
    assert seen == [baseline]                      # the dedup read's transaction ended before the PUT


async def test_the_broker_publish_holds_no_db_connection(client: AsyncClient, quiet: None, db: AsyncSession,
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    """A publish can wait up to its 5 s timeout while the broker is slow or down. db.refresh(doc) had just opened a
    read transaction, so every upload, duplicate requeue and reprocess pinned a pool connection idle in transaction
    for that long: ten at once starve the API, the starvation class the S3 fixes above removed."""
    seen: list[tuple[str, int]] = []

    async def recording_publish(*args: Any) -> None:
        seen.append(("ingest", checked_out()))

    async def recording_insights_publish(*args: Any) -> None:
        seen.append(("insights", checked_out()))

    monkeypatch.setattr(svc, "aenqueue_ingest", recording_publish)
    monkeypatch.setattr(svc, "aenqueue_insights", recording_insights_publish)

    async def set_state(sql: str) -> int:
        await db.execute(text(f"UPDATE documents SET {sql}"))
        await db.commit()
        return checked_out()

    baseline = checked_out()
    r = await upload(client, "a.txt", TEXT)
    assert r.status_code == 202, r.text
    doc_id = r.json()["document"]["id"]
    expected = [("ingest", baseline)]

    baseline = await set_state("status = 'failed', error_code = 'QUEUE_UNAVAILABLE', error_retryable = true")
    r = await upload(client, "a.txt", TEXT)                              # the same bytes: a requeued duplicate
    assert r.status_code == 202 and r.json()["requeued"] is True, r.text
    expected.append(("ingest", baseline))

    baseline = await set_state("status = 'ready', insights_status = 'failed'")
    r = await client.post(f"/api/v1/documents/{doc_id}/reprocess", json={"scope": "insights"}, headers=DEMO)
    assert r.status_code == 202, r.text
    expected.append(("insights", baseline))

    baseline = await set_state("status = 'ready', insights_status = 'ready'")
    r = await client.post(f"/api/v1/documents/{doc_id}/reprocess", json={"scope": "all"}, headers=DEMO)
    assert r.status_code == 202, r.text
    expected.append(("ingest", baseline))

    assert seen == expected


async def test_an_orphan_is_deleted_without_holding_a_db_connection(client: AsyncClient, quiet: None,
                                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    """When Postgres refuses the new row, the uploaded object is deleted from S3 (boto retries included). The failed
    session's connection used to stay checked out for that whole delete."""
    seen: list[int] = []
    real_delete = storage.delete

    def delete(key: str) -> None:
        seen.append(checked_out())
        real_delete(key)

    monkeypatch.setattr(storage, "delete", delete)
    monkeypatch.setattr(svc, "filename_title", lambda name: "bad\x00title")      # a value Postgres refuses
    baseline = checked_out()
    r = await upload(client, "a.txt", TEXT)

    assert r.status_code == 500
    assert seen == [baseline]
