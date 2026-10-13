"""End to end through the API (eager Celery runs the real tasks inline): upload -> extract -> chunk -> embed -> ready,
resumable embedding, and failure codes surfacing on GET /documents/{id}."""

import pytest
from httpx import AsyncClient

from app.ai.fake_provider import FakeProvider
from app.ai.provider import ProviderError
from app.config import settings
from app.services import storage
from app.worker.dispatch import QueueUnavailable
from tests.conftest import DEMO, upload
from tests.ingestion.helpers import (
    chunks_of,
    count,
    encrypt_pdf,
    get_doc,
    make_docx,
    make_image_only_pdf,
    make_mixed_pdf,
    make_pdf,
    runs_of,
)


async def _get(client: AsyncClient, doc_id: str) -> dict:
    r = await client.get(f"/api/v1/documents/{doc_id}", headers=DEMO)
    assert r.status_code == 200, r.text
    return r.json()


async def _upload_id(client: AsyncClient, name: str, data: bytes) -> str:
    r = await upload(client, name, data)
    assert r.status_code == 202, r.text
    return r.json()["document"]["id"]


async def test_pdf_upload_to_ready_with_pages_chunks_usage_and_timings(client: AsyncClient) -> None:
    doc_id = await _upload_id(client, "Larkspur_Handbook.pdf", make_pdf())
    doc = await _get(client, doc_id)
    assert doc["status"] == "ready", doc
    assert doc["page_count"] == 3
    assert doc["chunk_count"] == 3          # three short pages -> one chunk each
    assert doc["token_count"] > 0
    assert doc["progress_pct"] == 100 and doc["stage"] is None
    assert doc["insights_status"] == "ready"
    assert doc["error"] is None

    page = (await client.get(f"/api/v1/documents/{doc_id}/pages/2", headers=DEMO)).json()
    assert page["label"] == "p. 2" and "LRK-4471" in page["text"]

    chunks = chunks_of(doc_id)
    assert [c.page_start for c in chunks] == [1, 2, 3]
    assert all(c.embedding is not None and c.embedding_model == settings.EMBED_MODEL for c in chunks)
    assert chunks[1].header == "Larkspur Handbook (p. 2)"

    usage = count("SELECT count(*) FROM ai_usage WHERE operation = 'embed_chunks' AND document_id = :d "
                  "AND owner_id IS NOT NULL AND request_id IS NOT NULL", d=doc_id)
    assert usage == 1                        # 3 chunks fit in one batch

    (run,) = runs_of(doc_id)
    assert run.status == "succeeded" and run.attempt == 1 and run.duration_ms is not None
    assert set(run.stage_ms) == {"download", "extract", "chunk", "embed"}
    assert run.request_id is not None and run.owner_id is not None
    assert [r.status for r in runs_of(doc_id, "insights")] == ["succeeded"]


async def test_docx_upload_gets_section_labels(client: AsyncClient) -> None:
    doc_id = await _upload_id(client, "policy.docx", make_docx())
    doc = await _get(client, doc_id)
    assert doc["status"] == "ready" and doc["page_count"] == 4
    page = (await client.get(f"/api/v1/documents/{doc_id}/pages/2", headers=DEMO)).json()
    assert page["label"] == "§ Paid Time Off"
    assert page["heading_path"] == "Leave Policy > Paid Time Off"
    assert chunks_of(doc_id)[1].header == "policy > Leave Policy > Paid Time Off (§ Paid Time Off)"


async def test_partly_scanned_pdf_is_ready_with_warning(client: AsyncClient) -> None:
    doc = await _get(client, await _upload_id(client, "mixed.pdf", make_mixed_pdf()))
    assert doc["status"] == "ready"
    assert "pages_without_text:1" in doc["warnings"]   # insights may append its own warnings later
    assert doc["page_count"] == 2 and doc["chunk_count"] == 1


@pytest.mark.parametrize(
    ("name", "make", "code"),
    [
        ("locked.pdf", lambda: encrypt_pdf(make_pdf(), user_password="s3cret"), "ENCRYPTED_PDF"),
        ("scan.pdf", make_image_only_pdf, "NO_EXTRACTABLE_TEXT"),
    ],
)
async def test_permanent_extraction_failures_surface_as_error(client: AsyncClient, name, make, code) -> None:
    doc = await _get(client, await _upload_id(client, name, make()))
    assert doc["status"] == "failed"
    assert doc["error"]["code"] == code
    assert doc["error"]["retryable"] is False
    assert doc["error"]["message"]
    assert doc["insights_status"] == "pending"


async def test_too_many_pages_fails_without_retry(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "MAX_PAGES", 2)
    doc_id = await _upload_id(client, "big.pdf", make_pdf())
    doc = await _get(client, doc_id)
    assert doc["error"] == {"code": "TOO_MANY_PAGES", "message": "The PDF has 3 pages; the limit is 2.",
                            "retryable": False}
    assert [(r.status, r.error_code) for r in runs_of(doc_id)] == [("failed", "TOO_MANY_PAGES")]


async def test_embedding_resumes_after_failure_without_duplicates_or_re_embedding(
    client: AsyncClient, fake: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "EMBED_BATCH_SIZE", 1)
    embed_calls = {"n": 0}
    real_embed = fake.embed

    def embed_failing_on_second_call(texts: list[str]):
        embed_calls["n"] += 1
        if embed_calls["n"] == 2:
            fake.fail_next.append(ProviderError("bad_request", "simulated: input rejected"))
        return real_embed(texts)

    monkeypatch.setattr(fake, "embed", embed_failing_on_second_call)

    doc_id = await _upload_id(client, "handbook.pdf", make_pdf())
    doc = await _get(client, doc_id)
    assert doc["status"] == "failed" and doc["error"]["code"] == "PROVIDER_BAD_REQUEST"
    before = chunks_of(doc_id)
    assert [c.embedding is not None for c in before] == [True, False, False]   # batch 1 committed, then stopped
    first_vector = list(before[0].embedding)

    r = await client.post(f"/api/v1/documents/{doc_id}/reprocess", headers=DEMO, json={})
    assert r.status_code == 202, r.text
    doc = await _get(client, doc_id)
    assert doc["status"] == "ready" and doc["chunk_count"] == 3

    after = chunks_of(doc_id)
    assert [c.chunk_index for c in after] == [0, 1, 2]                          # no duplicate chunks
    assert all(c.embedding is not None for c in after)
    assert list(after[0].embedding) == first_vector                            # reused by embed_hash
    assert embed_calls["n"] == 4            # run 1: 2 calls (2nd failed); run 2: only the 2 missing chunks
    assert count("SELECT count(*) FROM ai_usage WHERE operation = 'embed_chunks' AND document_id = :d",
                 d=doc_id) == 3             # one paid batch per embedded chunk, the reused one is not paid twice


async def test_missing_original_file_is_storage_error(client: AsyncClient) -> None:
    doc_id = await _upload_id(client, "a.txt", b"Employees receive 18 PTO days per year.")
    storage.delete(get_doc(doc_id).storage_key)
    assert (await client.post(f"/api/v1/documents/{doc_id}/reprocess", headers=DEMO, json={})).status_code == 202
    doc = await _get(client, doc_id)
    assert doc["status"] == "failed" and doc["error"]["code"] == "STORAGE_ERROR"


async def test_insights_enqueue_failure_keeps_document_ready(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broker_down(*_args, **_kwargs) -> None:
        raise QueueUnavailable("connection refused")

    monkeypatch.setattr("app.ingestion.pipeline.enqueue_insights", broker_down)
    doc = await _get(client, await _upload_id(client, "a.txt", b"Employees receive 18 PTO days per year."))
    assert doc["status"] == "ready"
    assert doc["insights_status"] == "failed" and doc["insights_error_code"] == "QUEUE_UNAVAILABLE"
