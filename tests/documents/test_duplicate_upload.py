"""A duplicate upload decides from a row it read earlier, so its writes must not trust that read: another request can
requeue or count the same document in between. The requeue repeats "failed and retryable" in its WHERE clause, and
upload_count is incremented in SQL. Each test reads the row in two sessions first and then writes, which is exactly
the interleaving two near-simultaneous requests produce. Rows are inserted directly and the publish is recorded, so
no pipeline runs."""

import hashlib
import uuid
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import async_session
from app.models import Document
from app.services import documents as svc
from app.services import state


@pytest.fixture
def published(monkeypatch: pytest.MonkeyPatch) -> list[uuid.UUID]:
    runs: list[uuid.UUID] = []

    async def record(document_id: uuid.UUID, run_id: uuid.UUID, request_id: str | None = None) -> None:
        runs.append(run_id)

    monkeypatch.setattr(svc, "aenqueue_ingest", record)
    return runs


async def add_document(db: AsyncSession, owner_id: uuid.UUID, **cols: Any) -> uuid.UUID:
    doc_id = uuid.uuid4()
    db.add(Document(id=doc_id, owner_id=owner_id, filename="a.txt", mime_type="text/plain", size_bytes=10,
                    sha256=hashlib.sha256(doc_id.bytes).hexdigest(), storage_key=f"uploads/{owner_id}/{doc_id}.txt",
                    current_run_id=uuid.uuid4(), **cols))
    await db.commit()
    return doc_id


async def read(session: AsyncSession, doc_id: uuid.UUID) -> Document:
    return await session.scalar(select(Document).where(Document.id == doc_id))


async def test_a_stale_failed_read_does_not_requeue_again(db: AsyncSession, demo_user_id: uuid.UUID,
                                                          published: list[uuid.UUID]) -> None:
    doc_id = await add_document(db, demo_user_id, status="failed", error_code="QUEUE_UNAVAILABLE",
                                error_retryable=True)
    winner_run = uuid.uuid4()
    async with async_session() as stale, async_session() as other:
        doc = await read(stale, doc_id)                            # failed + retryable, as this request saw it
        await other.execute(state.requeue_stmt(doc_id, winner_run))  # ...but another re-upload requeued it first
        await other.commit()
        result = await svc._handle_duplicate(stale, doc)

    assert (result.duplicate, result.requeued, published) == (True, False, [])
    async with async_session() as check:
        row = await read(check, doc_id)
    assert (row.status, row.current_run_id, row.upload_count) == ("queued", winner_run, 2)   # its run stands


async def test_a_retryable_failure_is_still_requeued(db: AsyncSession, demo_user_id: uuid.UUID,
                                                     published: list[uuid.UUID]) -> None:
    doc_id = await add_document(db, demo_user_id, status="failed", error_code="QUEUE_UNAVAILABLE",
                                error_retryable=True)
    async with async_session() as session:
        result = await svc._handle_duplicate(session, await read(session, doc_id))

    assert result.requeued is True and published == [result.document.current_run_id]
    assert (result.document.status, result.document.upload_count) == ("queued", 2)


@pytest.mark.parametrize("cols", [{"status": "ready"},
                                  {"status": "failed", "error_code": "QUEUE_UNAVAILABLE", "error_retryable": True}])
async def test_concurrent_duplicates_are_both_counted(db: AsyncSession, demo_user_id: uuid.UUID,
                                                      published: list[uuid.UUID], cols: dict[str, Any]) -> None:
    doc_id = await add_document(db, demo_user_id, **cols)
    async with async_session() as first, async_session() as second:
        doc_a, doc_b = await read(first, doc_id), await read(second, doc_id)    # both see upload_count = 1
        await svc._handle_duplicate(first, doc_a)
        result = await svc._handle_duplicate(second, doc_b)

    assert result.document.upload_count == 3
    assert len(published) == (1 if cols["status"] == "failed" else 0)          # only one of them requeued
