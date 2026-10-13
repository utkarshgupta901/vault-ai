"""run_insights against real Postgres with the FakeProvider: the fenced upsert, the fields copied onto the
document, the ai_usage row, and the failure mapping the Celery task relies on."""

import uuid
from datetime import date
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select, text

from app.ai.fake_provider import FakeProvider
from app.ai.insights import RETRY_OUTPUT_TOKENS, run_insights
from app.ai.provider import ProviderError
from app.config import settings
from app.core.db import async_session, sync_session
from app.core.errors import ErrorCode, PermanentError
from app.models import AIUsage, Document, DocumentInsight
from tests.insights.helpers import canned_insights, sql, start_insights_run, upload_ready


async def _state(document_id: uuid.UUID) -> tuple[Document, DocumentInsight | None]:
    async with async_session() as s:
        return await s.get(Document, document_id), await s.get(DocumentInsight, document_id)


async def _usage_rows(task_id: str) -> list[AIUsage]:
    async with async_session() as s:
        return list(await s.scalars(select(AIUsage).where(AIUsage.task_id == task_id)))


def _spy_json(fake: FakeProvider) -> list[dict[str, Any]]:
    """Record the kwargs of every complete_json call (FakeProvider only logs calls that succeed)."""
    seen: list[dict[str, Any]] = []
    original = fake.complete_json

    def spy(**kwargs: Any) -> Any:
        seen.append(kwargs)
        return original(**kwargs)

    fake.complete_json = spy  # type: ignore[method-assign]
    return seen


async def test_run_insights_writes_verified_insights_and_marks_ready(client: AsyncClient, fake: FakeProvider,
                                                                     demo_user_id: uuid.UUID) -> None:
    doc_id = await upload_ready(client)
    run_id = await start_insights_run(doc_id)
    fake.json_responses["DocumentInsights"] = canned_insights()

    timings = run_insights(doc_id, run_id, task_id="task-1", request_id="req-1")

    assert set(timings) == {"load", "llm", "verify", "write"}
    doc, ins = await _state(doc_id)
    assert ins is not None
    # verified insights row
    assert ins.run_id == run_id and ins.coverage == "full"
    assert ins.model == settings.INSIGHTS_MODEL and ins.prompt_version == "insights.v1"
    assert len(ins.risk_flags) == 1                                   # the paraphrased flag was dropped
    assert "Supplier’s" in ins.risk_flags[0]["quote"]                 # stored in the document's own words
    assert ins.risk_flags[0]["page"] == 1
    assert ins.key_points == [{"point": "Either party may terminate for convenience with 60 days' written notice",
                               "page": 1}]                            # deduped; page 7 clamped + verified
    assert ins.tags == ["contract", "auto-renewal"]
    assert ins.entities == [{"name": "Brightline", "type": "organization"}]
    assert len(ins.suggested_questions) == 5
    assert ins.document_date == date(2026, 3, 1)
    assert ins.injection_suspected is True                            # regex caught Appendix C
    # fields copied onto the document + status
    assert doc.insights_status == "ready" and doc.insights_error_code is None
    assert doc.title == "Brightline MSA" and doc.category == "contract"
    assert doc.tags == ["contract", "auto-renewal"] and doc.document_date == date(2026, 3, 1)
    assert "risk_flags_dropped:1" in doc.warnings
    # one ledger row, traced to the task and request
    [usage] = await _usage_rows("task-1")
    assert usage.operation == "insights" and usage.prompt_version == "insights.v1"
    assert usage.owner_id == demo_user_id and usage.document_id == doc_id and usage.request_id == "req-1"
    assert usage.input_tokens > 0


async def test_reprocess_upserts_the_same_row_and_does_not_stack_warnings(client: AsyncClient,
                                                                         fake: FakeProvider) -> None:
    doc_id = await upload_ready(client)
    fake.json_responses["DocumentInsights"] = canned_insights()
    run_insights(doc_id, await start_insights_run(doc_id), task_id="t1", request_id=None)

    second_run = await start_insights_run(doc_id, new_run=True)
    fake.json_responses["DocumentInsights"] = canned_insights(title="Brightline MSA v2", risk_flags=[
        {"type": "x", "severity": "low", "page": 1, "quote": "Made-up sentence that is not in the document."},
        {"type": "y", "severity": "low", "page": 1, "quote": "Another invented clause about penalties and fines."},
    ])
    run_insights(doc_id, second_run, task_id="t2", request_id=None)

    doc, ins = await _state(doc_id)
    assert await sql("SELECT count(*) FROM document_insights WHERE document_id = :id", id=doc_id) == 1
    assert ins.run_id == second_run and ins.title == "Brightline MSA v2" and ins.risk_flags == []
    assert doc.title == "Brightline MSA v2"
    assert [w for w in doc.warnings if str(w).startswith("risk_flags_dropped")] == ["risk_flags_dropped:2"]


async def test_empty_title_keeps_the_document_title(client: AsyncClient, fake: FakeProvider) -> None:
    doc_id = await upload_ready(client)
    before = (await _state(doc_id))[0].title
    fake.json_responses["DocumentInsights"] = canned_insights(title="   ")
    run_insights(doc_id, await start_insights_run(doc_id), task_id="t1", request_id=None)
    doc, ins = await _state(doc_id)
    assert ins.title is None and doc.title == before


async def test_stale_run_id_spends_nothing_and_writes_nothing(client: AsyncClient, fake: FakeProvider) -> None:
    doc_id = await upload_ready(client)
    await start_insights_run(doc_id)
    seen = _spy_json(fake)

    timings = run_insights(doc_id, uuid.uuid4(), task_id="stale", request_id=None)

    assert set(timings) == {"load"} and seen == []
    doc, ins = await _state(doc_id)
    assert ins is None and doc.insights_status == "processing"
    assert await _usage_rows("stale") == []


async def test_run_superseded_during_llm_call_writes_only_usage(client: AsyncClient, fake: FakeProvider) -> None:
    doc_id = await upload_ready(client)
    run_id = await start_insights_run(doc_id)

    def reprocess_while_thinking(_: str) -> Any:
        with sync_session() as s:   # simulates POST /reprocess landing while the model is working
            s.execute(text("UPDATE documents SET current_run_id = gen_random_uuid() WHERE id = :id"), {"id": doc_id})
        return canned_insights()

    fake.json_responses["DocumentInsights"] = reprocess_while_thinking
    run_insights(doc_id, run_id, task_id="superseded", request_id=None)

    doc, ins = await _state(doc_id)
    assert ins is None and doc.insights_status == "processing" and doc.title != "Brightline MSA"
    assert len(await _usage_rows("superseded")) == 1   # the call was paid for, so it is still in the ledger


async def test_deleted_document_is_a_quiet_no_op(client: AsyncClient, fake: FakeProvider) -> None:
    doc_id = await upload_ready(client)
    run_id = await start_insights_run(doc_id)
    await sql("DELETE FROM documents WHERE id = :id", id=doc_id)
    assert set(run_insights(doc_id, run_id, task_id="gone", request_id=None)) == {"load"}


async def test_truncated_output_retries_once_with_a_bigger_budget(client: AsyncClient, fake: FakeProvider) -> None:
    doc_id = await upload_ready(client)
    run_id = await start_insights_run(doc_id)
    seen = _spy_json(fake)
    fake.fail_next.append(ProviderError("incomplete", "max_output_tokens"))
    fake.json_responses["DocumentInsights"] = canned_insights()

    run_insights(doc_id, run_id, task_id="t1", request_id=None)

    assert [c["max_output_tokens"] for c in seen] == [8000, RETRY_OUTPUT_TOKENS]
    assert (await _state(doc_id))[0].insights_status == "ready"


@pytest.mark.parametrize(("errors", "code", "calls"), [
    ([ProviderError("incomplete", "cut"), ProviderError("incomplete", "cut")], ErrorCode.INSIGHTS_TRUNCATED, 2),
    ([ProviderError("bad_request", "no parsable output")] * 2, ErrorCode.INSIGHTS_PARSE_FAILED, 2),
    ([ProviderError("refusal", "I can't help with that")], ErrorCode.INSIGHTS_REFUSED, 1),
    ([ProviderError("bad_request", "invalid model", status_code=400)], ErrorCode.PROVIDER_BAD_REQUEST, 1),
])
async def test_permanent_failures_map_to_insights_codes(client: AsyncClient, fake: FakeProvider,
                                                        errors: list[ProviderError], code: ErrorCode,
                                                        calls: int) -> None:
    doc_id = await upload_ready(client)
    run_id = await start_insights_run(doc_id)
    seen = _spy_json(fake)
    fake.fail_next.extend(errors)

    with pytest.raises(PermanentError) as exc_info:
        run_insights(doc_id, run_id, task_id="t1", request_id=None)

    assert exc_info.value.code == code
    assert len(seen) == calls
    assert (await _state(doc_id))[1] is None


async def test_transient_errors_go_back_to_celery(client: AsyncClient, fake: FakeProvider) -> None:
    doc_id = await upload_ready(client)
    run_id = await start_insights_run(doc_id)
    fake.fail_next.append(ProviderError("transient", "429", retry_after=7))
    with pytest.raises(ProviderError) as exc_info:
        run_insights(doc_id, run_id, task_id="t1", request_id=None)
    assert exc_info.value.kind == "transient" and exc_info.value.retry_after == 7


async def test_long_document_is_sampled(client: AsyncClient, fake: FakeProvider,
                                        monkeypatch: pytest.MonkeyPatch) -> None:
    doc_id = await upload_ready(client)
    run_id = await start_insights_run(doc_id)
    await sql("INSERT INTO document_pages (document_id, page_no, label, text, char_count) "
              "SELECT :id, n, 'p. ' || n, 'filler text for page ' || n, 20 FROM generate_series(2, 30) n", id=doc_id)
    monkeypatch.setattr(settings, "INSIGHTS_MAX_INPUT_TOKENS", 200)
    fake.json_responses["DocumentInsights"] = canned_insights()
    seen = _spy_json(fake)

    run_insights(doc_id, run_id, task_id="t1", request_id=None)

    prompt_input = seen[0]["input"]
    assert 'pages="30" coverage="sampled"' in prompt_input
    assert '<page n="1"' in prompt_input and '<page n="30"' in prompt_input
    assert (await _state(doc_id))[1].coverage == "sampled"
