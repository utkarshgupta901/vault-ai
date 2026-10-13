"""Chat sessions: scope, starter questions, warnings, ownership and pagination."""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.pagination import encode_cursor
from tests.chat.helpers import API, insert_document, new_session, set_suggested_questions, upload_ready
from tests.conftest import DEMO, OTHER

HANDBOOK = "Larkspur employees receive 18 PTO days per year. PTO requests need manager approval."


async def test_create_session_for_documents_with_starters(
    client: AsyncClient, db: AsyncSession, demo_user_id: uuid.UUID
) -> None:
    a = await insert_document(db, demo_user_id, "a.txt", ["alpha text"])
    b = await insert_document(db, demo_user_id, "b.txt", ["beta text"])
    await set_suggested_questions(db, a, ["A1?", "A2?", "A3?"])
    await set_suggested_questions(db, b, ["B1?", "a1?", "B2?", "B3?"])
    s = await new_session(client, [str(a), str(b)], title="Compare")
    assert s["document_ids"] == [str(a), str(b)] and s["all_documents"] is False and s["title"] == "Compare"
    assert s["starter_questions"] == ["A1?", "B1?", "A2?", "A3?", "B2?"]     # round-robin, deduped, max 5
    assert s["warnings"] == []


async def test_all_documents_session_warns_when_some_not_ready(
    client: AsyncClient, db: AsyncSession, demo_user_id: uuid.UUID
) -> None:
    await insert_document(db, demo_user_id, "ready.txt", ["alpha"])
    await insert_document(db, demo_user_id, "busy.txt", ["beta"], status="processing")
    s = await new_session(client)
    assert s["all_documents"] is True and s["document_ids"] == []
    assert s["warnings"] == ["docs_not_ready"]


async def test_failed_document_does_not_warn_docs_not_ready(
    client: AsyncClient, db: AsyncSession, demo_user_id: uuid.UUID
) -> None:
    """Regression: a failed upload (e.g. a scanned PDF with no text) made every all-documents session and answer say
    documents were "still processing". A failed document never becomes searchable, so it is not 'not ready yet'."""
    await insert_document(db, demo_user_id, "ready.txt", ["Larkspur employees receive 18 PTO days per year."])
    await insert_document(db, demo_user_id, "scan.pdf", ["(no text)"], status="failed")
    s = await new_session(client)
    assert s["warnings"] == []
    assert (await client.get(f"{API}/chat/sessions/{s['id']}", headers=DEMO)).json()["warnings"] == []
    r = await client.post(f"{API}/chat/sessions/{s['id']}/messages",
                          json={"content": "How many PTO days do Larkspur employees receive?"}, headers=DEMO)
    assert r.status_code == 200, r.text
    assert r.json()["answer"]["warnings"] == []


async def test_no_ready_documents_is_409(client: AsyncClient, db: AsyncSession, demo_user_id: uuid.UUID) -> None:
    r = await client.post(f"{API}/chat/sessions", json={"all_documents": True}, headers=DEMO)
    assert r.status_code == 409 and r.json()["error"]["code"] == "NO_READY_DOCUMENTS"
    busy = await insert_document(db, demo_user_id, "busy.txt", ["beta"], status="queued")
    r = await client.post(f"{API}/chat/sessions", json={"document_ids": [str(busy)]}, headers=DEMO)
    assert r.status_code == 409 and r.json()["error"]["code"] == "NO_READY_DOCUMENTS"
    assert (await client.get(f"{API}/chat/sessions", headers=DEMO)).json()["items"] == []   # nothing half-created


async def test_foreign_or_unknown_document_is_404(client: AsyncClient, db: AsyncSession,
                                                  other_user_id: uuid.UUID) -> None:
    theirs = await insert_document(db, other_user_id, "theirs.txt", ["secret"])
    for doc_id in (theirs, uuid.uuid4()):
        r = await client.post(f"{API}/chat/sessions", json={"document_ids": [str(doc_id)]}, headers=DEMO)
        assert r.status_code == 404 and r.json()["error"]["code"] == "DOCUMENT_NOT_FOUND"


async def test_empty_scope_is_422(client: AsyncClient) -> None:
    r = await client.post(f"{API}/chat/sessions", json={}, headers=DEMO)
    assert r.status_code == 422


async def test_foreign_session_is_404_everywhere(client: AsyncClient) -> None:
    await upload_ready(client, "handbook.txt", HANDBOOK)
    sid = (await new_session(client))["id"]
    base = f"{API}/chat/sessions/{sid}"
    for method, path, body in [("GET", "", None), ("GET", "/messages", None), ("DELETE", "", None),
                               ("POST", "/messages", {"content": "hi"}), ("POST", "/messages/stream", {"content": "hi"})]:
        r = await client.request(method, base + path, json=body, headers=OTHER)
        assert r.status_code == 404, (method, path)
        assert r.json()["error"]["code"] == "SESSION_NOT_FOUND"
    assert (await client.get(base, headers=DEMO)).status_code == 200


async def test_list_get_delete(client: AsyncClient) -> None:
    await upload_ready(client, "handbook.txt", HANDBOOK)
    ids = [(await new_session(client))["id"] for _ in range(3)]
    page1 = (await client.get(f"{API}/chat/sessions?limit=2", headers=DEMO)).json()
    assert [s["id"] for s in page1["items"]] == ids[::-1][:2]
    page2 = (await client.get(f"{API}/chat/sessions?limit=2&cursor={page1['next_cursor']}", headers=DEMO)).json()
    assert [s["id"] for s in page2["items"]] == [ids[0]] and page2["next_cursor"] is None
    assert (await client.get(f"{API}/chat/sessions", headers=OTHER)).json()["items"] == []
    assert (await client.delete(f"{API}/chat/sessions/{ids[0]}", headers=DEMO)).status_code == 204
    assert (await client.get(f"{API}/chat/sessions/{ids[0]}", headers=DEMO)).status_code == 404


async def test_session_with_recent_message_lists_first(client: AsyncClient) -> None:
    await upload_ready(client, "handbook.txt", HANDBOOK)
    older = (await new_session(client))["id"]
    newer = (await new_session(client))["id"]
    r = await client.post(f"{API}/chat/sessions/{older}/messages",
                          json={"content": "How many PTO days do Larkspur employees receive per year?"}, headers=DEMO)
    assert r.status_code == 200, r.text
    items = (await client.get(f"{API}/chat/sessions", headers=DEMO)).json()["items"]
    assert [s["id"] for s in items] == [older, newer]
    assert items[0]["title"] == "How many PTO days do Larkspur employees receive per year?"[:60]
    assert items[0]["last_message_at"] is not None


@pytest.mark.parametrize("payload", [
    {"t": 123, "id": "00000000-0000-0000-0000-000000000000"},          # TypeError in fromisoformat
    {"t": "2026-10-13T10:00:00+00:00", "id": 5},                       # AttributeError in uuid.UUID
    {"t": "not a time", "id": "00000000-0000-0000-0000-000000000000"},
    {"t": "2026-10-13T10:00:00+00:00"},
])
async def test_malformed_sessions_cursor_is_400(client: AsyncClient, payload: dict) -> None:
    r = await client.get(f"{API}/chat/sessions", params={"cursor": encode_cursor(payload)}, headers=DEMO)
    assert r.status_code == 400 and r.json()["error"]["code"] == "INVALID_CURSOR"
