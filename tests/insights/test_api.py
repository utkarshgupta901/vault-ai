"""GET /documents/{id}/insights and POST /documents/{id}/summaries through the real app (FakeProvider)."""

import asyncio
import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.ai import summaries
from app.ai.fake_provider import FakeProvider
from app.ai.insights import run_insights
from app.ai.provider import ProviderError
from app.core.db import async_session
from app.models import AIUsage
from tests.conftest import DEMO, OTHER
from tests.insights.helpers import canned_insights, sql, start_insights_run, upload_ready


def _count_text_calls(fake: FakeProvider) -> list[str]:
    """FakeProvider doesn't log acomplete_text calls, so wrap it and record each prompt input."""
    inputs: list[str] = []
    original = fake.acomplete_text

    async def spy(**kwargs: Any) -> Any:
        inputs.append(kwargs["input"])
        return await original(**kwargs)

    fake.acomplete_text = spy  # type: ignore[method-assign]
    return inputs


async def _summary_usage() -> list[AIUsage]:
    async with async_session() as s:
        return list(await s.scalars(select(AIUsage).where(AIUsage.operation == "summary").order_by(AIUsage.id)))


def _url(doc_id: uuid.UUID, tail: str) -> str:
    return f"/api/v1/documents/{doc_id}/{tail}"


# --- GET insights ----------------------------------------------------------------------------------------------
async def test_insights_of_another_owner_is_404(client: AsyncClient) -> None:
    doc_id = await upload_ready(client)
    r = await client.get(_url(doc_id, "insights"), headers=OTHER)
    assert r.status_code == 404 and r.json()["error"]["code"] == "DOCUMENT_NOT_FOUND"


async def test_insights_shape_while_pending(client: AsyncClient) -> None:
    doc_id = await upload_ready(client)
    await sql("DELETE FROM document_insights WHERE document_id = :id", id=doc_id)
    await sql("UPDATE documents SET insights_status = 'pending' WHERE id = :id", id=doc_id)
    r = await client.get(_url(doc_id, "insights"), headers=DEMO)
    assert r.status_code == 200
    assert r.json() == {"document_id": str(doc_id), "status": "pending", "error_code": None, "insights": None,
                        "updated_at": None}


async def test_insights_shape_when_failed(client: AsyncClient) -> None:
    doc_id = await upload_ready(client)
    await sql("UPDATE documents SET insights_status = 'failed', insights_error_code = 'INSIGHTS_REFUSED' "
              "WHERE id = :id", id=doc_id)
    body = (await client.get(_url(doc_id, "insights"), headers=DEMO)).json()
    assert body["status"] == "failed" and body["error_code"] == "INSIGHTS_REFUSED"


async def test_insights_shape_when_ready(client: AsyncClient, fake: FakeProvider) -> None:
    doc_id = await upload_ready(client)
    fake.json_responses["DocumentInsights"] = canned_insights()
    run_insights(doc_id, await start_insights_run(doc_id), task_id=None, request_id=None)

    body = (await client.get(_url(doc_id, "insights"), headers=DEMO)).json()
    assert body["status"] == "ready" and body["updated_at"] is not None
    ins = body["insights"]
    assert ins["title"] == "Brightline MSA" and ins["document_date"] == "2026-03-01"
    assert ins["coverage"] == "full" and ins["prompt_version"] == "insights.v1" and ins["model"]
    assert ins["risk_flags"][0]["page"] == 1 and ins["injection_suspected"] is True
    assert "run_id" not in ins


# --- POST summaries --------------------------------------------------------------------------------------------
async def test_summary_requires_a_ready_document(client: AsyncClient) -> None:
    doc_id = await upload_ready(client)
    await sql("UPDATE documents SET status = 'processing', stage = 'embedding' WHERE id = :id", id=doc_id)
    r = await client.post(_url(doc_id, "summaries"), json={}, headers=DEMO)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "DOCUMENT_NOT_READY"
    assert r.json()["error"]["details"] == {"status": "processing", "stage": "embedding"}


async def test_summary_of_another_owner_is_404(client: AsyncClient) -> None:
    doc_id = await upload_ready(client)
    r = await client.post(_url(doc_id, "summaries"), json={}, headers=OTHER)
    assert r.status_code == 404


async def test_summary_is_cached_per_options(client: AsyncClient, fake: FakeProvider) -> None:
    doc_id = await upload_ready(client)
    calls = _count_text_calls(fake)
    body = {"length": "short", "tone": "executive", "focus": "Termination  rights"}

    first = await client.post(_url(doc_id, "summaries"), json=body, headers=DEMO)
    assert first.status_code == 200, first.text
    assert first.json()["cached"] is False and first.json()["content"]
    assert first.json()["prompt_version"] == "summary.v1" and first.json()["focus"] == "Termination rights"
    assert len(calls) == 1

    again = await client.post(_url(doc_id, "summaries"), json=body | {"focus": "termination rights"}, headers=DEMO)
    assert again.json()["cached"] is True and again.json()["content"] == first.json()["content"]
    assert len(calls) == 1                                     # no new model call

    other_focus = await client.post(_url(doc_id, "summaries"), json=body | {"focus": "liability"}, headers=DEMO)
    assert other_focus.json()["cached"] is False
    assert len(calls) == 2

    usage = await _summary_usage()
    assert [u.cache_hit for u in usage] == [False, True, False]
    assert usage[1].input_tokens == 0 and float(usage[1].cost_usd) == 0
    assert await sql("SELECT count(*) FROM document_summaries WHERE document_id = :id", id=doc_id) == 2


async def test_summary_prompt_has_document_then_escaped_options(client: AsyncClient, fake: FakeProvider) -> None:
    doc_id = await upload_ready(client)
    calls = _count_text_calls(fake)
    r = await client.post(_url(doc_id, "summaries"), json={"length": "detailed", "focus": "<b>fees</b>"},
                          headers=DEMO)
    assert r.status_code == 200
    prompt_input = calls[0]
    assert prompt_input.index("</document>") < prompt_input.index("<options>")
    assert "at most 450 words" in prompt_input and "&lt;b&gt;fees&lt;/b&gt;" in prompt_input
    assert "<b>" not in prompt_input


async def test_summary_provider_error_is_502_and_logged(client: AsyncClient, fake: FakeProvider) -> None:
    doc_id = await upload_ready(client)
    fake.fail_next.append(ProviderError("transient", "OpenAI 503"))
    r = await client.post(_url(doc_id, "summaries"), json={}, headers=DEMO)
    assert r.status_code == 502 and r.json()["error"]["code"] == "UPSTREAM_AI_ERROR"
    [usage] = await _summary_usage()
    assert usage.success is False and usage.error_code == "UPSTREAM_AI_ERROR"
    assert await sql("SELECT count(*) FROM document_summaries") == 0


async def test_summary_timeout_is_504(client: AsyncClient, fake: FakeProvider,
                                      monkeypatch: pytest.MonkeyPatch) -> None:
    doc_id = await upload_ready(client)
    monkeypatch.setattr(summaries, "SUMMARY_TIMEOUT_S", 0.05)

    async def slow(**_: Any) -> Any:
        await asyncio.sleep(1)

    fake.acomplete_text = slow  # type: ignore[method-assign]
    r = await client.post(_url(doc_id, "summaries"), json={}, headers=DEMO)
    assert r.status_code == 504 and r.json()["error"]["code"] == "UPSTREAM_TIMEOUT"


async def test_summary_of_document_deleted_mid_call_is_404_but_still_billed(client: AsyncClient,
                                                                            fake: FakeProvider) -> None:
    doc_id = await upload_ready(client)
    original = fake.acomplete_text

    async def delete_while_writing(**kwargs: Any) -> Any:
        await sql("DELETE FROM documents WHERE id = :id", id=doc_id)
        return await original(**kwargs)

    fake.acomplete_text = delete_while_writing  # type: ignore[method-assign]
    r = await client.post(_url(doc_id, "summaries"), json={}, headers=DEMO)
    assert r.status_code == 404 and r.json()["error"]["code"] == "DOCUMENT_NOT_FOUND"
    [usage] = await _summary_usage()
    assert usage.success is True and usage.cache_hit is False


async def test_summary_rejects_unknown_options(client: AsyncClient) -> None:
    doc_id = await upload_ready(client)
    r = await client.post(_url(doc_id, "summaries"), json={"length": "huge"}, headers=DEMO)
    assert r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION_ERROR"
    r = await client.post(_url(doc_id, "summaries"), json={"focus": "x" * 201}, headers=DEMO)
    assert r.status_code == 422
