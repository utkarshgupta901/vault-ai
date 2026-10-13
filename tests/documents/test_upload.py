import io
import zipfile

from httpx import AsyncClient

from tests.conftest import DEMO, OTHER, upload

TEXT = b"Larkspur Analytics handbook. Employees receive 18 PTO days per year. Incident LRK-4471."


async def test_upload_txt_is_processed_and_ready(client: AsyncClient) -> None:
    r = await upload(client, "handbook.txt", TEXT)
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["duplicate"] is False
    assert r.headers["location"] == f"/api/v1/documents/{body['document']['id']}"
    doc = (await client.get(body["links"]["self"], headers=DEMO)).json()
    assert doc["status"] == "ready"          # eager Celery ran the pipeline inline
    assert doc["chunk_count"] >= 1
    assert doc["mime_type"] == "text/plain"


async def test_duplicate_upload_returns_200_without_new_work(client: AsyncClient) -> None:
    first = await upload(client, "a.txt", TEXT)
    second = await upload(client, "renamed.txt", TEXT)
    assert second.status_code == 200
    assert second.json()["duplicate"] is True
    assert second.json()["document"]["id"] == first.json()["document"]["id"]
    assert second.json()["document"]["upload_count"] == 2


async def test_same_bytes_different_owner_is_a_separate_document(client: AsyncClient) -> None:
    a = await upload(client, "a.txt", TEXT, DEMO)
    b = await upload(client, "a.txt", TEXT, OTHER)
    assert b.status_code == 202
    assert a.json()["document"]["id"] != b.json()["document"]["id"]


async def test_other_owner_gets_404(client: AsyncClient) -> None:
    doc_id = (await upload(client, "a.txt", TEXT)).json()["document"]["id"]
    for path in ("", "/pages/1", "/file", "/events"):
        r = await client.get(f"/api/v1/documents/{doc_id}{path}", headers=OTHER)
        assert r.status_code == 404, path
        assert r.json()["error"]["code"] == "DOCUMENT_NOT_FOUND"


async def test_empty_file_rejected(client: AsyncClient) -> None:
    r = await upload(client, "empty.txt", b"")
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "EMPTY_FILE"


async def test_binary_garbage_rejected_as_unsupported(client: AsyncClient) -> None:
    r = await upload(client, "image.png", b"\x89PNG\r\n\x1a\n\x00\x00\x00binary")
    assert r.status_code == 415
    assert r.json()["error"]["code"] == "UNSUPPORTED_MEDIA_TYPE"


async def test_zip_that_is_not_docx_rejected(client: AsyncClient) -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("hello.txt", "hi")
    r = await upload(client, "fake.docx", buf.getvalue())
    assert r.status_code == 415


async def test_upload_without_key_rejected_before_body(client: AsyncClient) -> None:
    r = await client.post("/api/v1/documents", files={"file": ("a.txt", TEXT, "text/plain")})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "UNAUTHENTICATED"


async def test_invalid_key_rejected(client: AsyncClient) -> None:
    r = await client.get("/api/v1/documents", headers={"X-API-Key": "nope"})
    assert r.status_code == 401


async def test_list_is_paginated_newest_first(client: AsyncClient) -> None:
    ids = [(await upload(client, f"d{i}.txt", TEXT + bytes([65 + i]))).json()["document"]["id"] for i in range(3)]
    page1 = (await client.get("/api/v1/documents?limit=2", headers=DEMO)).json()
    assert [d["id"] for d in page1["items"]] == ids[::-1][:2]
    page2 = (await client.get(f"/api/v1/documents?limit=2&cursor={page1['next_cursor']}", headers=DEMO)).json()
    assert [d["id"] for d in page2["items"]] == [ids[0]]
    assert page2["next_cursor"] is None


async def test_bad_cursor_is_400(client: AsyncClient) -> None:
    r = await client.get("/api/v1/documents?cursor=@@@", headers=DEMO)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INVALID_CURSOR"


async def test_delete_removes_document(client: AsyncClient) -> None:
    doc_id = (await upload(client, "a.txt", TEXT)).json()["document"]["id"]
    assert (await client.delete(f"/api/v1/documents/{doc_id}", headers=DEMO)).status_code == 204
    assert (await client.get(f"/api/v1/documents/{doc_id}", headers=DEMO)).status_code == 404


async def test_page_text_available_after_ready(client: AsyncClient) -> None:
    doc_id = (await upload(client, "a.txt", TEXT)).json()["document"]["id"]
    r = await client.get(f"/api/v1/documents/{doc_id}/pages/1", headers=DEMO)
    assert r.status_code == 200
    assert "18 PTO days" in r.json()["text"]


async def test_events_stream_reports_terminal_status(client: AsyncClient) -> None:
    doc_id = (await upload(client, "a.txt", TEXT)).json()["document"]["id"]
    r = await client.get(f"/api/v1/documents/{doc_id}/events", headers=DEMO)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    assert "event: status" in r.text and '"status":"ready"' in r.text
