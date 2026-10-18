"""The ask pipeline end to end (FakeProvider, real Postgres/Valkey): SSE contract, JSON endpoint, gate, multi-turn,
idempotency, SESSION_BUSY, fallback model, time budgets and client disconnects."""

import asyncio
import base64
import re
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import date
from functools import partial
from typing import Any

import anyio
import anyio.lowlevel
import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import set_provider
from app.ai.fake_provider import FakeProvider
from app.ai.provider import ProviderError, StreamEnd, Usage
from app.config import Settings, settings
from app.core.pagination import encode_cursor
from app.schemas.chat import CitationsEvent, MessageCreate, TokenEvent
from app.services import chat as svc
from tests.chat.helpers import API, ask_json, ask_stream, new_session, parse_sse, upload_ready
from tests.conftest import DEMO

HANDBOOK = ("Larkspur employees receive 18 PTO days per year. After three years of service Larkspur employees "
            "receive 22 PTO days per year. PTO requests need manager approval.")
QUESTION = "How many PTO days do Larkspur employees receive per year?"


@pytest.fixture
async def session_id(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> str:
    # FakeProvider's bag-of-words similarities aren't calibrated like real embeddings (and depend on the chunk
    # header wording of the ingestion pipeline), so pin the gate low: on-topic questions pass, while an off-topic
    # question (no shared words, similarity ~0) is still refused.
    monkeypatch.setattr(settings, "GATE_MIN_SIMILARITY", 0.1)
    await upload_ready(client, "handbook.txt", HANDBOOK)
    return (await new_session(client))["id"]


def _names(events: list[tuple[str, dict]]) -> list[str]:
    return [name for name, _ in events]


async def _rows(db: AsyncSession, sql: str, **params: Any) -> list[Any]:
    return list((await db.execute(text(sql), params)).mappings().all())


# --- SSE contract ----------------------------------------------------------------------------------------------
async def test_stream_event_contract(client: AsyncClient, session_id: str, db: AsyncSession, fake: FakeProvider) -> None:
    r = await ask_stream(client, session_id, QUESTION)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    events = parse_sse(r.text)
    names = _names(events)
    assert names[0] == "meta" and names[-1] == "done"
    assert names == ["meta", *["token"] * names.count("token"), "citations", "followups", "done"]
    assert names.count("token") >= 2

    meta, citations, done = events[0][1], events[-3][1], events[-1][1]
    assert meta["sources"][0]["ref"] == "S1" and meta["sources"][0]["filename"] == "handbook.txt"
    assert meta["standalone_query"] is None and meta["retrieval_mode"] == "hybrid" and meta["warnings"] == []
    answer = "".join(d["delta"] for n, d in events if n == "token")
    assert answer == "Based on the documents, here is the answer [S1]."
    assert citations["answer_type"] == "answered"
    assert [c["ref"] for c in citations["citations"]] == ["S1"]
    assert citations["signals"]["valid_citations"] == 1 and citations["signals"]["gate"] is False
    assert citations["confidence"] in ("low", "medium", "high")
    assert done["message_id"] == meta["message_id"] and done["model"] == settings.ANSWER_MODEL
    assert done["usage"]["output_tokens"] > 0 and done["usage"]["estimated"] is False
    assert set(done["timings"]) >= {"embed", "retrieve", "ttft", "total"} and "condense" not in done["timings"]

    msgs = (await client.get(f"{API}/chat/sessions/{session_id}/messages", headers=DEMO)).json()["items"]
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    saved = msgs[1]
    assert saved["id"] == meta["message_id"] and saved["status"] == "complete" and saved["content"] == answer
    assert saved["reply_to_id"] == msgs[0]["id"] == meta["user_message_id"]
    assert saved["prompt_version"] == "answer.v2" and saved["model"] == settings.ANSWER_MODEL
    assert saved["followups"] == events[-2][1]["questions"]
    assert saved["citations"][0]["chunk_id"] == meta["sources"][0]["chunk_id"]
    assert set(saved["citations"][0]) >= {"ref", "chunk_id", "document_id", "filename", "label", "page_start",
                                           "page_end", "snippet", "weak_support"}
    assert set(saved["signals"]) == {"top_similarity", "id_hit", "lexical_match", "valid_citations",
                                     "invalid_citations", "uncited_claims", "ungrounded_facts", "gate"}
    retrieval = saved["retrieval"]
    assert {"mode", "gate", "top_similarity", "terms", "id_terms", "candidates"} <= set(retrieval)
    assert {"chunk_id", "ref", "rrf", "sem_rank", "lex_rank", "sem_dist", "id_hit"} <= set(retrieval["candidates"][0])

    usage = await _rows(db, "SELECT operation, message_id, cache_hit, prompt_version FROM ai_usage "
                            "WHERE operation IN ('embed_query', 'answer', 'followups') ORDER BY id")
    assert [u["operation"] for u in usage] == ["embed_query", "answer", "followups"]
    assert all(str(u["message_id"]) == meta["message_id"] for u in usage)
    assert usage[1]["prompt_version"] == "answer.v2"


async def test_json_endpoint_returns_saved_pair(client: AsyncClient, session_id: str) -> None:
    r = await ask_json(client, session_id, QUESTION)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["user_message"]["role"] == "user" and body["user_message"]["content"] == QUESTION
    answer = body["answer"]
    assert answer["status"] == "complete" and answer["answer_type"] == "answered"
    assert answer["citations"][0]["ref"] == "S1" and answer["followups"]
    assert body["answer"]["seq"] > body["user_message"]["seq"]


async def test_off_topic_question_is_gated_without_a_model_call(
    client: AsyncClient, session_id: str, fake: FakeProvider
) -> None:
    r = await ask_stream(client, session_id, "Quantum chromodynamics gluon confinement lattice?")
    events = parse_sse(r.text)
    assert _names(events) == ["meta", "token", "citations", "done"]       # no follow-ups for a gate refusal
    assert events[0][1]["sources"] == []
    reply = events[1][1]["delta"]
    assert reply.startswith("I couldn't find this in your documents. Your documents cover: ")
    assert events[2][1]["answer_type"] == "not_found" and events[2][1]["signals"]["gate"] is True
    assert events[2][1]["confidence"] == "low"
    assert not [c for c in fake.calls if c["op"] == "stream"]
    assert events[3][1]["model"] is None and events[3][1]["usage"]["cost_usd"] in (0, "0")


async def test_identifier_hit_bypasses_the_gate(
    client: AsyncClient, fake: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "GATE_MIN_SIMILARITY", 0.99)     # similarity alone would always gate
    await upload_ready(client, "ops.txt", "Incident LRK-4471 was a payroll outage.")
    sid = (await new_session(client))["id"]
    events = parse_sse((await ask_stream(client, sid, "LRK-4471 xyzzy plugh frobnicate quux?")).text)
    signals = events[_names(events).index("citations")][1]["signals"]
    assert signals["id_hit"] is True and signals["gate"] is False
    assert [c for c in fake.calls if c["op"] == "stream"]


async def test_dates_shown_in_the_prompt_are_grounded(
    client: AsyncClient, db: AsyncSession, fake: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: an answer quoting its source's document date and "today" (as answer.v2 rule 5 asks) had those
    dates flagged as ungrounded. The checker must ground exactly what the prompt showed, so the clock here moves to
    the next day after the prompt is built (a turn straddling midnight): checking against a fresh date.today() would
    flag the answer again."""
    monkeypatch.setattr(settings, "GATE_MIN_SIMILARITY", 0.1)
    days = iter([date(2026, 10, 13), date(2026, 10, 14), date(2026, 10, 15)])
    monkeypatch.setattr(Settings, "today", lambda self: next(days))
    doc_id = await upload_ready(client, "handbook.txt", HANDBOOK)
    await db.execute(text("UPDATE documents SET document_date = '2026-01-15' WHERE id = :id"), {"id": doc_id})
    await db.commit()

    def answer(prompt: str) -> str:
        shown_today = re.search(r"Today's date: (\S+)", prompt).group(1)          # type: ignore[union-attr]
        doc_date = re.search(r'<source id="S1"[^>]* date="([^"]+)"', prompt).group(1)  # type: ignore[union-attr]
        return (f"The handbook dated {doc_date} gives Larkspur employees 18 PTO days per year, "
                f"as of today, {shown_today} [S1].")

    fake.answer_override = answer
    body = (await ask_json(client, (await new_session(client))["id"], QUESTION)).json()["answer"]
    assert body["content"].startswith("The handbook dated 2026-01-15 ") and "today, 2026-10-13 [S1]" in body["content"]
    assert body["signals"]["ungrounded_facts"] == 0 and body["signals"]["uncited_claims"] == 0
    assert body["citations"][0]["weak_support"] is False


async def test_an_odd_month_spelling_does_not_fail_the_turn(
    client: AsyncClient, session_id: str, fake: FakeProvider
) -> None:
    """Regression: "Auguſt" (long s, U+017F) matched the checker's case-insensitive month pattern but not its month
    table; the KeyError failed the turn after the whole answer had streamed (status 'failed', INTERNAL). Such text can
    come from a document (the corpus has injection probes) or from a user asking the model to echo it."""
    fake.answer_override = lambda _: "Larkspur employees receive 18 PTO days per year, as of Auguſt 2026 [S1]."
    r = await ask_json(client, session_id, QUESTION)
    assert r.status_code == 200, r.text
    answer = r.json()["answer"]
    assert answer["status"] == "complete" and answer["answer_type"] == "answered"
    assert answer["signals"]["ungrounded_facts"] == 1                 # 2026 is checked as a number; the handbook has none


async def test_model_refusal_is_not_found(client: AsyncClient, session_id: str, fake: FakeProvider) -> None:
    fake.answer_override = lambda _: "I couldn’t find this in your documents. The sources cover PTO."
    body = (await ask_json(client, session_id, QUESTION)).json()["answer"]
    assert body["answer_type"] == "not_found" and body["confidence"] == "low"
    assert body["signals"]["gate"] is False


# --- multi-turn ------------------------------------------------------------------------------------------------
async def test_multi_turn_condenses_from_the_second_question(
    client: AsyncClient, session_id: str, fake: FakeProvider, db: AsyncSession
) -> None:
    seen: list[str] = []

    def condense(text_in: str) -> dict:
        seen.append(text_in)
        return {"query": f"Larkspur PTO days per year, turn {len(seen) + 1}"}

    fake.json_responses["CondensedQuery"] = condense
    assert (await ask_json(client, session_id, QUESTION)).status_code == 200
    assert seen == []                                                   # no history on turn 1: no condense call
    second = (await ask_json(client, session_id, "And after three years?")).json()["answer"]
    assert len(seen) == 1 and second["standalone_query"] == "Larkspur PTO days per year, turn 2"
    third_events = parse_sse((await ask_stream(client, session_id, "And for contractors?")).text)
    assert third_events[0][1]["standalone_query"] == "Larkspur PTO days per year, turn 3"

    history = seen[1]                                                   # turn 3 saw turns 1 and 2, in seq order
    order = [history.index(s) for s in (QUESTION, "Based on the documents", "And after three years?")]
    assert order == sorted(order) and "[S1]" not in history
    assert "<latest_message>And for contractors?</latest_message>" in history
    usage = await _rows(db, "SELECT count(*) AS n FROM ai_usage WHERE operation = 'condense' AND success")
    assert usage[0]["n"] == 2


async def test_condense_failure_falls_back_to_previous_query(
    client: AsyncClient, session_id: str, fake: FakeProvider
) -> None:
    assert (await ask_json(client, session_id, QUESTION)).status_code == 200
    fake.fail_next.append(ProviderError("transient", "condense down"))   # next provider call = condense
    events = parse_sse((await ask_stream(client, session_id, "and contractors?")).text)
    meta = events[0][1]
    assert "condense_fallback" in meta["warnings"]
    assert meta["standalone_query"] == f"{QUESTION} and contractors?"
    assert _names(events)[-1] == "done"


# --- idempotency and concurrency -------------------------------------------------------------------------------
async def test_completed_client_message_id_is_replayed(
    client: AsyncClient, session_id: str, fake: FakeProvider
) -> None:
    first = (await ask_json(client, session_id, QUESTION, client_message_id="c-1")).json()
    streams_before = len([c for c in fake.calls if c["op"] == "stream"])
    replay = parse_sse((await ask_stream(client, session_id, QUESTION, client_message_id="c-1")).text)
    assert _names(replay) == ["meta", "token", "citations", "followups", "done"]
    assert replay[0][1]["message_id"] == first["answer"]["id"]
    assert replay[1][1]["delta"] == first["answer"]["content"]
    assert replay[0][1]["sources"][0]["ref"] == "S1"
    assert len([c for c in fake.calls if c["op"] == "stream"]) == streams_before    # no new model call
    again = (await ask_json(client, session_id, QUESTION, client_message_id="c-1")).json()
    assert again["answer"]["id"] == first["answer"]["id"]
    msgs = (await client.get(f"{API}/chat/sessions/{session_id}/messages", headers=DEMO)).json()["items"]
    assert len(msgs) == 2


async def test_client_message_id_still_streaming_is_409(
    client: AsyncClient, session_id: str, db: AsyncSession
) -> None:
    user_id = uuid.uuid4()
    await db.execute(text("INSERT INTO chat_messages (id, session_id, role, content, client_message_id) "
                          "VALUES (:id, :s, 'user', 'q', 'c-2')"), {"id": user_id, "s": session_id})
    await db.execute(text("INSERT INTO chat_messages (session_id, role, status, reply_to_id) "
                          "VALUES (:s, 'assistant', 'streaming', :u)"), {"s": session_id, "u": user_id})
    await db.commit()
    r = await ask_stream(client, session_id, "q", client_message_id="c-2")
    assert r.status_code == 409 and r.json()["error"]["code"] == "MESSAGE_IN_PROGRESS"


async def test_failed_client_message_id_is_regenerated_on_the_same_user_row(
    client: AsyncClient, session_id: str, db: AsyncSession
) -> None:
    user_id = uuid.uuid4()
    await db.execute(text("INSERT INTO chat_messages (id, session_id, role, content, client_message_id) "
                          "VALUES (:id, :s, 'user', :q, 'c-3')"), {"id": user_id, "s": session_id, "q": QUESTION})
    await db.execute(text("INSERT INTO chat_messages (session_id, role, status, content, reply_to_id) "
                          "VALUES (:s, 'assistant', 'failed', 'partial', :u)"), {"s": session_id, "u": user_id})
    await db.commit()
    body = (await ask_json(client, session_id, QUESTION, client_message_id="c-3")).json()
    assert body["user_message"]["id"] == str(user_id) and body["answer"]["reply_to_id"] == str(user_id)
    msgs = (await client.get(f"{API}/chat/sessions/{session_id}/messages", headers=DEMO)).json()["items"]
    assert [m["status"] for m in msgs] == ["complete", "failed", "complete"]


async def test_a_legacy_empty_complete_answer_is_regenerated_not_replayed(
    client: AsyncClient, session_id: str, db: AsyncSession
) -> None:
    """Before empty streams became failures, an answer with no text was saved as an empty 'complete' message, and
    such rows are still in existing databases: a retry of its client_message_id replayed the empty answer forever."""
    first = (await ask_json(client, session_id, QUESTION, client_message_id="c-legacy")).json()
    await db.execute(text("UPDATE chat_messages SET content = '' WHERE id = :id"), {"id": first["answer"]["id"]})
    await db.commit()
    retry = (await ask_json(client, session_id, QUESTION, client_message_id="c-legacy")).json()
    assert retry["answer"]["status"] == "complete" and retry["answer"]["content"].strip()
    assert retry["answer"]["id"] != first["answer"]["id"]                      # regenerated, not replayed ...
    assert retry["user_message"]["id"] == first["user_message"]["id"]          # ... on the same user row


async def test_second_ask_while_streaming_is_session_busy(
    client: AsyncClient, session_id: str, db: AsyncSession
) -> None:
    await db.execute(text("INSERT INTO chat_messages (session_id, role, status) VALUES (:s, 'assistant', 'streaming')"),
                     {"s": session_id})
    await db.commit()
    for r in (await ask_stream(client, session_id, QUESTION), await ask_json(client, session_id, QUESTION)):
        assert r.status_code == 409 and r.json()["error"]["code"] == "SESSION_BUSY"
    msgs = (await client.get(f"{API}/chat/sessions/{session_id}/messages", headers=DEMO)).json()["items"]
    assert [m["role"] for m in msgs] == ["assistant"]                   # the rejected ask left no rows behind


async def test_stale_streaming_row_is_expired_then_ask_proceeds(
    client: AsyncClient, session_id: str, db: AsyncSession
) -> None:
    await db.execute(text("INSERT INTO chat_messages (session_id, role, status, created_at) "
                          "VALUES (:s, 'assistant', 'streaming', now() - interval '10 minutes')"), {"s": session_id})
    await db.commit()
    assert (await ask_json(client, session_id, QUESTION)).status_code == 200
    statuses = await _rows(db, "SELECT status FROM chat_messages WHERE role = 'assistant' ORDER BY seq")
    assert [s["status"] for s in statuses] == ["interrupted", "complete"]


async def test_ask_after_scope_documents_deleted_is_409(client: AsyncClient) -> None:
    doc_id = await upload_ready(client, "handbook.txt", HANDBOOK)
    sid = (await new_session(client, [doc_id]))["id"]
    assert (await client.delete(f"{API}/documents/{doc_id}", headers=DEMO)).status_code == 204
    r = await ask_stream(client, sid, QUESTION)
    assert r.status_code == 409 and r.json()["error"]["code"] == "NO_READY_DOCUMENTS"


# --- fallback, budgets, disconnects ----------------------------------------------------------------------------
async def test_fallback_model_on_provider_error_before_first_token(
    client: AsyncClient, session_id: str, fake: FakeProvider, db: AsyncSession
) -> None:
    # Warm the query-embedding cache via /search, so the next provider call of the ask is the answer stream.
    assert (await client.get(f"{API}/search", params={"q": QUESTION}, headers=DEMO)).status_code == 200
    fake.fail_next.append(ProviderError("transient", "OpenAI server error (500)."))
    events = parse_sse((await ask_stream(client, session_id, QUESTION)).text)
    assert _names(events)[-1] == "done"
    assert events[-1][1]["model"] == settings.FAST_MODEL
    answer = (await client.get(f"{API}/chat/sessions/{session_id}/messages", headers=DEMO)).json()["items"][-1]
    assert answer["warnings"] == ["fallback_model"] and answer["model"] == settings.FAST_MODEL
    rows = await _rows(db, "SELECT operation, model, success, error_code, cache_hit FROM ai_usage "
                           "WHERE message_id = :m ORDER BY id", m=answer["id"])
    assert [(r["operation"], r["model"], r["success"]) for r in rows[:3]] == [
        ("embed_query", settings.EMBED_MODEL, True),
        ("answer", settings.ANSWER_MODEL, False),
        ("answer", settings.FAST_MODEL, True)]
    assert rows[0]["cache_hit"] is True and rows[1]["error_code"] == "UPSTREAM_AI_ERROR"


class ScriptedProvider(FakeProvider):
    """FakeProvider whose answer stream follows a script per model: a delay before the first token, a delay
    between tokens, and the tokens themselves."""

    def __init__(self, *, first_delay: dict[str, float] | None = None, gap: float = 0.0) -> None:
        super().__init__()
        self.first_delay = first_delay or {}
        self.gap = gap

    async def astream_text(self, *, model: str, effort: str, instructions: str, input: Any,
                           max_output_tokens: int) -> AsyncIterator[str | StreamEnd]:
        self.calls.append({"op": "stream", "model": model})
        await asyncio.sleep(self.first_delay.get(model, 0.0))
        yield "Employees receive 18 PTO days per year "
        await asyncio.sleep(self.gap)
        yield "[S1]."
        yield StreamEnd(usage=Usage(model=model, input_tokens=100, output_tokens=12))


async def test_no_first_token_in_time_falls_back(
    client: AsyncClient, session_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "ANSWER_TTFT_TIMEOUT_S", 0.2)
    set_provider(ScriptedProvider(first_delay={settings.ANSWER_MODEL: 5.0}))
    events = parse_sse((await ask_stream(client, session_id, QUESTION)).text)
    assert _names(events)[-1] == "done" and events[-1][1]["model"] == settings.FAST_MODEL
    answer = (await client.get(f"{API}/chat/sessions/{session_id}/messages", headers=DEMO)).json()["items"][-1]
    assert answer["warnings"] == ["fallback_model"] and answer["content"] == "Employees receive 18 PTO days per year [S1]."


async def test_mid_stream_stall_emits_error_and_saves_partial(
    client: AsyncClient, session_id: str, monkeypatch: pytest.MonkeyPatch, db: AsyncSession
) -> None:
    monkeypatch.setattr(settings, "ANSWER_IDLE_TIMEOUT_S", 0.2)
    set_provider(ScriptedProvider(gap=5.0))
    events = parse_sse((await ask_stream(client, session_id, QUESTION)).text)
    assert _names(events) == ["meta", "token", "error"]
    assert events[-1][1]["code"] == "UPSTREAM_TIMEOUT"
    answer = (await client.get(f"{API}/chat/sessions/{session_id}/messages", headers=DEMO)).json()["items"][-1]
    assert answer["status"] == "failed" and answer["content"] == "Employees receive 18 PTO days per year "
    rows = await _rows(db, "SELECT estimated, success FROM ai_usage WHERE operation = 'answer'")
    assert rows == [{"estimated": True, "success": False}]
    assert (await ask_json(client, session_id, "next question")).status_code != 409   # session not left busy


async def test_json_deadline_is_504_and_saves_failed(
    client: AsyncClient, session_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "JSON_ANSWER_TIMEOUT_S", 0.3)
    set_provider(ScriptedProvider(gap=5.0))
    r = await ask_json(client, session_id, QUESTION)
    assert r.status_code == 504 and r.json()["error"]["code"] == "UPSTREAM_TIMEOUT"
    answer = (await client.get(f"{API}/chat/sessions/{session_id}/messages", headers=DEMO)).json()["items"][-1]
    assert answer["status"] == "failed" and answer["content"].startswith("Employees receive")


async def _turn(client: AsyncClient, session_id: str, demo_user_id: uuid.UUID) -> svc.Turn:
    return await svc.start_turn(demo_user_id, uuid.UUID(session_id), MessageCreate(content=QUESTION))


async def test_client_disconnect_saves_interrupted_with_estimated_usage(
    client: AsyncClient, session_id: str, demo_user_id: uuid.UUID, db: AsyncSession
) -> None:
    set_provider(ScriptedProvider(gap=5.0))
    turn = await _turn(client, session_id, demo_user_id)
    events = svc.answer(turn)
    async for event in events:
        if isinstance(event, TokenEvent):
            break
    await events.aclose()                       # what the SSE layer does when the client goes away
    msg = (await _rows(db, "SELECT status, content FROM chat_messages WHERE id = :id", id=turn.assistant_message_id))[0]
    assert msg["status"] == "interrupted" and msg["content"] == "Employees receive 18 PTO days per year "
    usage = await _rows(db, "SELECT estimated, input_tokens, output_tokens FROM ai_usage "
                            "WHERE operation = 'answer' AND message_id = :id", id=turn.assistant_message_id)
    assert usage[0]["estimated"] is True and usage[0]["input_tokens"] > 0 and usage[0]["output_tokens"] > 0


async def test_cancelled_task_saves_interrupted(
    client: AsyncClient, session_id: str, demo_user_id: uuid.UUID, db: AsyncSession
) -> None:
    set_provider(ScriptedProvider(gap=5.0))
    turn = await _turn(client, session_id, demo_user_id)
    got_token = asyncio.Event()

    async def consume() -> None:
        async for event in svc.answer(turn):
            if isinstance(event, TokenEvent):
                got_token.set()

    task = asyncio.create_task(consume())
    await asyncio.wait_for(got_token.wait(), 5)
    task.cancel()                               # cancellation lands inside the provider's sleep
    with pytest.raises(asyncio.CancelledError):
        await task
    msg = (await _rows(db, "SELECT status FROM chat_messages WHERE id = :id", id=turn.assistant_message_id))[0]
    assert msg["status"] == "interrupted"


async def test_keyword_only_when_query_embedding_fails(
    client: AsyncClient, session_id: str, fake: FakeProvider
) -> None:
    fake.fail_next.append(ProviderError("transient", "embeddings down"))    # first provider call = embed_query
    events = parse_sse((await ask_stream(client, session_id, QUESTION)).text)
    assert events[0][1]["retrieval_mode"] == "keyword" and "keyword_only" in events[0][1]["warnings"]
    assert _names(events)[-1] == "done"


async def test_messages_page_newest_in_chronological_order(client: AsyncClient, session_id: str) -> None:
    for i in range(3):
        assert (await ask_json(client, session_id, f"{QUESTION} ({i})")).status_code == 200
    base = f"{API}/chat/sessions/{session_id}/messages"
    page1 = (await client.get(f"{base}?limit=4", headers=DEMO)).json()
    seqs = [m["seq"] for m in page1["items"]]
    assert seqs == sorted(seqs) and len(seqs) == 4
    assert [m["role"] for m in page1["items"]] == ["user", "assistant", "user", "assistant"]
    assert page1["items"][-1]["content"] and page1["next_cursor"]
    page2 = (await client.get(f"{base}?limit=4&cursor={page1['next_cursor']}", headers=DEMO)).json()
    assert [m["seq"] for m in page2["items"]] == sorted(m["seq"] for m in page2["items"])
    assert max(m["seq"] for m in page2["items"]) < seqs[0] and len(page2["items"]) == 2
    assert page2["next_cursor"] is None


# --- review regressions ----------------------------------------------------------------------------------------
async def test_off_topic_question_with_schedule_a_is_still_gated(
    client: AsyncClient, fake: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: "schedule a" became the identifier phrase 'schedule <-> a', which Postgres parses to just
    'schedul', so any document mentioning a schedule counted as an exact identifier hit and skipped the gate."""
    monkeypatch.setattr(settings, "GATE_MIN_SIMILARITY", 0.99)     # similarity alone would always gate
    await upload_ready(client, "payroll.txt", "The payroll schedule is monthly. Salaries are paid on the last day.")
    sid = (await new_session(client))["id"]
    events = parse_sse((await ask_stream(client, sid, "Can you schedule a dentist appointment for me?")).text)
    signals = events[_names(events).index("citations")][1]["signals"]
    assert signals["id_hit"] is False and signals["gate"] is True
    assert not [c for c in fake.calls if c["op"] == "stream"]          # refused without paying for an answer


async def test_client_message_id_reused_for_a_different_question_is_422(
    client: AsyncClient, session_id: str, fake: FakeProvider
) -> None:
    """Regression: the old answer was replayed (200) for a NEW question sent with a reused idempotency key."""
    first = (await ask_json(client, session_id, QUESTION, client_message_id="c-9")).json()
    streams_before = len([c for c in fake.calls if c["op"] == "stream"])
    for send in (ask_json, ask_stream):
        r = await send(client, session_id, "Where is the office?", client_message_id="c-9")
        assert r.status_code == 422, r.text
        error = r.json()["error"]
        assert error["code"] == "VALIDATION_ERROR" and "client_message_id" in error["message"]
    assert len([c for c in fake.calls if c["op"] == "stream"]) == streams_before    # no model call
    again = (await ask_json(client, session_id, QUESTION, client_message_id="c-9")).json()   # a true retry: replay
    assert again["answer"]["id"] == first["answer"]["id"]
    msgs = (await client.get(f"{API}/chat/sessions/{session_id}/messages", headers=DEMO)).json()["items"]
    assert len(msgs) == 2


class TruncatingProvider(FakeProvider):
    """An answer stream that ends with status 'incomplete' (max_output_tokens hit), as the OpenAI provider reports
    response.incomplete."""

    async def astream_text(self, *, model: str, effort: str, instructions: str, input: Any,
                           max_output_tokens: int) -> AsyncIterator[str | StreamEnd]:
        self.calls.append({"op": "stream", "model": model})
        yield "Employees receive 18 PTO days "
        yield StreamEnd(usage=Usage(model=model, input_tokens=100, output_tokens=4000), status="incomplete")


async def test_truncated_answer_gets_a_truncated_warning(client: AsyncClient, session_id: str) -> None:
    """Regression: an answer cut off by max_output_tokens was saved as a normal complete answer with no warning."""
    set_provider(TruncatingProvider())
    answer = (await ask_json(client, session_id, QUESTION, client_message_id="c-t")).json()["answer"]
    assert answer["status"] == "complete" and answer["content"] == "Employees receive 18 PTO days "
    assert answer["warnings"] == ["truncated"]
    replay = parse_sse((await ask_stream(client, session_id, QUESTION, client_message_id="c-t")).text)
    assert replay[0][1]["warnings"] == ["truncated"]                   # a replay says so too


async def test_long_previous_answer_still_gives_condense_its_history(
    client: AsyncClient, session_id: str, fake: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: an answer longer than HISTORY_MAX_TOKENS emptied the history, so the follow-up skipped condense
    and was searched (and answered) as typed."""
    monkeypatch.setattr(settings, "HISTORY_MAX_TOKENS", 60)
    fake.answer_override = lambda _: "Larkspur employees receive 18 PTO days per year [S1]. " * 20
    seen: list[str] = []

    def condense(text_in: str) -> dict:
        seen.append(text_in)
        return {"query": "Larkspur PTO days after three years"}

    fake.json_responses["CondensedQuery"] = condense
    assert (await ask_json(client, session_id, QUESTION)).status_code == 200
    second = (await ask_json(client, session_id, "And after three years?")).json()["answer"]
    assert len(seen) == 1 and second["standalone_query"] == "Larkspur PTO days after three years"
    assert QUESTION in seen[0] and "Larkspur employees receive 18 PTO days" in seen[0]   # question + cut answer


async def test_malformed_messages_cursor_is_400(client: AsyncClient, session_id: str) -> None:
    """Regression: {"seq": 1e20} reached Postgres ("bigint out of range") and came back as a 500 INTERNAL."""
    infinity = base64.urlsafe_b64encode(b'{"seq": Infinity}').decode().rstrip("=")
    bad = [encode_cursor({"seq": 10**20}), encode_cursor({"seq": 0}), encode_cursor({"seq": -5}),
           encode_cursor({"seq": "5"}), encode_cursor({"seq": None}), encode_cursor({"seq": True}),
           encode_cursor({"seq": 1.5}), encode_cursor({"other": 1}), infinity]
    for cursor in bad:
        r = await client.get(f"{API}/chat/sessions/{session_id}/messages", params={"cursor": cursor}, headers=DEMO)
        assert r.status_code == 400 and r.json()["error"]["code"] == "INVALID_CURSOR", cursor


class HangingJsonProvider(FakeProvider):
    """FakeProvider whose structured-output call for one schema hangs, so a test can cancel the turn mid-call."""

    def __init__(self, schema: str) -> None:
        super().__init__()
        self.schema = schema
        self.started = asyncio.Event()

    async def acomplete_json(self, **kwargs: Any) -> Any:
        if kwargs["schema"].__name__ == self.schema:
            self.started.set()
            await asyncio.sleep(30)
        return self.complete_json(**kwargs)


async def _cancel_mid_call(turn: svc.Turn, provider: HangingJsonProvider) -> None:
    async def drain() -> None:
        async for _ in svc.answer(turn):
            pass

    task = asyncio.create_task(drain())
    await asyncio.wait_for(provider.started.wait(), 5)
    task.cancel()                               # what a client disconnect does to the request task
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_followups_call_cut_off_by_a_disconnect_is_recorded(
    client: AsyncClient, session_id: str, demo_user_id: uuid.UUID, db: AsyncSession
) -> None:
    """Hard rule 7: the provider bills a call we abandoned, so it still gets an (estimated) ai_usage row."""
    provider = HangingJsonProvider("FollowupQuestions")
    set_provider(provider)
    turn = await _turn(client, session_id, demo_user_id)
    await _cancel_mid_call(turn, provider)
    msg = (await _rows(db, "SELECT status FROM chat_messages WHERE id = :id", id=turn.assistant_message_id))[0]
    assert msg["status"] == "complete"                                  # the answer was saved before follow-ups
    rows = await _rows(db, "SELECT estimated, success, input_tokens, prompt_version FROM ai_usage "
                           "WHERE operation = 'followups' AND message_id = :id", id=turn.assistant_message_id)
    assert len(rows) == 1 and rows[0]["estimated"] is True and rows[0]["success"] is False
    assert rows[0]["input_tokens"] > 0 and rows[0]["prompt_version"] == "followups.v1"


async def test_condense_call_cut_off_by_a_disconnect_is_recorded(
    client: AsyncClient, session_id: str, demo_user_id: uuid.UUID, db: AsyncSession
) -> None:
    assert (await ask_json(client, session_id, QUESTION)).status_code == 200      # history, so turn 2 condenses
    provider = HangingJsonProvider("CondensedQuery")
    set_provider(provider)
    turn = await svc.start_turn(demo_user_id, uuid.UUID(session_id), MessageCreate(content="And after three years?"))
    await _cancel_mid_call(turn, provider)
    msg = (await _rows(db, "SELECT status FROM chat_messages WHERE id = :id", id=turn.assistant_message_id))[0]
    assert msg["status"] == "interrupted"
    rows = await _rows(db, "SELECT estimated, success, input_tokens FROM ai_usage "
                           "WHERE operation = 'condense' AND message_id = :id", id=turn.assistant_message_id)
    assert len(rows) == 1 and rows[0]["estimated"] is True and rows[0]["success"] is False
    assert rows[0]["input_tokens"] > 0


# --- failure paths: what the client sees, what is saved, what is billed -----------------------------------------
class PerModelStreamProvider(FakeProvider):
    """FakeProvider whose answer stream for a scripted model replays a fixed list: text deltas, a StreamEnd, or an
    exception raised at that point. A model without a script answers like FakeProvider."""

    def __init__(self, scripts: dict[str, list[str | StreamEnd | Exception]]) -> None:
        super().__init__()
        self.scripts = scripts

    async def astream_text(self, *, model: str, effort: str, instructions: str, input: Any,
                           max_output_tokens: int) -> AsyncIterator[str | StreamEnd]:
        if model not in self.scripts:
            async for item in super().astream_text(model=model, effort=effort, instructions=instructions,
                                                   input=input, max_output_tokens=max_output_tokens):
                yield item
            return
        self.calls.append({"op": "stream", "model": model})
        for item in self.scripts[model]:
            if isinstance(item, Exception):
                raise item
            yield item


VENDOR_TEXT = "Incorrect API key provided: vendor-detail-7731"


def _end(model: str, *, status: str = "completed", output_tokens: int = 12) -> StreamEnd:
    return StreamEnd(usage=Usage(model=model, input_tokens=100, output_tokens=output_tokens), status=status)


async def _last_answer(client: AsyncClient, session_id: str) -> dict[str, Any]:
    return (await client.get(f"{API}/chat/sessions/{session_id}/messages", headers=DEMO)).json()["items"][-1]


async def _answer_usage_rows(db: AsyncSession, message_id: Any) -> list[Any]:
    return await _rows(db, "SELECT model, success, error_code, estimated, input_tokens, output_tokens FROM ai_usage "
                           "WHERE operation = 'answer' AND message_id = :m ORDER BY id", m=message_id)


async def test_provider_error_text_never_reaches_the_client(
    client: AsyncClient, session_id: str, db: AsyncSession
) -> None:
    """Regression: the `error` event and the JSON 502 quoted the vendor's own error text (a 401 explains what is wrong
    with the API key). The client gets the code and a generic message; the detail goes to the logs only."""
    error = ProviderError("bad_request", f"OpenAI rejected the request (401): {VENDOR_TEXT}", status_code=401)
    set_provider(PerModelStreamProvider({settings.ANSWER_MODEL: [error], settings.FAST_MODEL: [error]}))
    sse = await ask_stream(client, session_id, QUESTION)
    events = parse_sse(sse.text)
    assert _names(events) == ["meta", "error"] and events[-1][1]["code"] == "UPSTREAM_AI_ERROR"
    as_json = await ask_json(client, session_id, QUESTION)
    assert as_json.status_code == 502 and as_json.json()["error"]["code"] == "UPSTREAM_AI_ERROR"
    history = await client.get(f"{API}/chat/sessions/{session_id}/messages", headers=DEMO)
    stored = await _rows(db, "SELECT m.*, u.error_code AS usage_error FROM chat_messages m "
                             "LEFT JOIN ai_usage u ON u.message_id = m.id")
    for seen in (sse.text, as_json.text, history.text, str(stored)):
        assert "vendor-detail-7731" not in seen and "API key" not in seen


async def test_both_models_failing_before_the_first_token_is_an_error_billed_at_zero(
    client: AsyncClient, session_id: str, db: AsyncSession
) -> None:
    """Neither attempt produced anything (5xx), so neither was billed: two zero-token rows. Regression: the second
    one was a full-prompt estimate, which inflated the daily quota and the cost metrics."""
    error = ProviderError("transient", "OpenAI server error (500).", status_code=500)
    set_provider(PerModelStreamProvider({settings.ANSWER_MODEL: [error], settings.FAST_MODEL: [error]}))
    events = parse_sse((await ask_stream(client, session_id, QUESTION)).text)
    assert _names(events) == ["meta", "error"] and events[-1][1]["code"] == "UPSTREAM_AI_ERROR"
    answer = await _last_answer(client, session_id)
    assert answer["status"] == "failed" and answer["content"] == "" and answer["warnings"] == ["fallback_model"]
    assert answer["input_tokens"] == 0 and answer["output_tokens"] == 0
    zero = {"success": False, "error_code": "UPSTREAM_AI_ERROR", "estimated": False, "input_tokens": 0,
            "output_tokens": 0}
    assert await _answer_usage_rows(db, answer["id"]) == [{"model": settings.ANSWER_MODEL, **zero},
                                                          {"model": settings.FAST_MODEL, **zero}]


async def test_provider_error_after_the_first_token_fails_without_fallback(
    client: AsyncClient, session_id: str, db: AsyncSession
) -> None:
    """Once text was shown, another model would contradict it: no fallback, the partial answer is kept as failed and
    billed as an estimate (the stream's own usage event never came)."""
    cut = ProviderError("transient", "OpenAI stream failed: response.failed")
    provider = PerModelStreamProvider({settings.ANSWER_MODEL: ["Employees receive ", "18 PTO days ", cut]})
    set_provider(provider)
    events = parse_sse((await ask_stream(client, session_id, QUESTION)).text)
    assert _names(events) == ["meta", "token", "token", "error"] and events[-1][1]["code"] == "UPSTREAM_AI_ERROR"
    assert [c["model"] for c in provider.calls if c["op"] == "stream"] == [settings.ANSWER_MODEL]
    answer = await _last_answer(client, session_id)
    assert answer["status"] == "failed" and answer["content"] == "Employees receive 18 PTO days "
    assert answer["warnings"] == []
    rows = await _answer_usage_rows(db, answer["id"])
    assert len(rows) == 1 and rows[0]["model"] == settings.ANSWER_MODEL and rows[0]["success"] is False
    assert rows[0]["error_code"] == "UPSTREAM_AI_ERROR" and rows[0]["estimated"] is True
    assert rows[0]["input_tokens"] > 0 and rows[0]["output_tokens"] > 0


@pytest.mark.parametrize("first_attempt", [
    pytest.param([_end(settings.ANSWER_MODEL, status="incomplete", output_tokens=4000)], id="no-text-budget-spent"),
    pytest.param([" ", "\n", _end(settings.ANSWER_MODEL, output_tokens=4000)], id="whitespace-only"),
    pytest.param([ProviderError("refusal", "The model refused.", usage=Usage(model=settings.ANSWER_MODEL,
                                                                             input_tokens=100, output_tokens=4000))],
                 id="refusal"),
])
async def test_first_attempt_without_answer_text_falls_back(
    client: AsyncClient, session_id: str, db: AsyncSession, first_attempt: list[Any]
) -> None:
    """Regression: a stream that ended without text (reasoning spent the whole output budget) was saved as an empty
    'complete' answer. Nothing was shown yet, so it is a failed first attempt like any other: fall back. Its row keeps
    the usage the provider reported, because those tokens were billed."""
    set_provider(PerModelStreamProvider({settings.ANSWER_MODEL: first_attempt}))
    events = parse_sse((await ask_stream(client, session_id, QUESTION)).text)
    assert _names(events)[-1] == "done" and events[-1][1]["model"] == settings.FAST_MODEL
    assert "".join(d["delta"] for n, d in events if n == "token").startswith("Based on the documents")
    answer = await _last_answer(client, session_id)
    assert answer["status"] == "complete" and answer["warnings"] == ["fallback_model"]
    first, second = await _answer_usage_rows(db, answer["id"])
    assert first == {"model": settings.ANSWER_MODEL, "success": False, "error_code": "UPSTREAM_AI_ERROR",
                     "estimated": False, "input_tokens": 100, "output_tokens": 4000}
    assert second["model"] == settings.FAST_MODEL and second["success"] is True


async def test_no_answer_text_from_either_model_is_failed_not_an_empty_complete(
    client: AsyncClient, session_id: str, db: AsyncSession
) -> None:
    """Regression: the empty answer was saved 'complete', so every retry with the same client_message_id replayed
    it. Now it is UPSTREAM_AI_ERROR, and the retry regenerates on the same user row."""
    set_provider(PerModelStreamProvider({
        settings.ANSWER_MODEL: [_end(settings.ANSWER_MODEL, status="incomplete", output_tokens=4000)],
        settings.FAST_MODEL: [" ", _end(settings.FAST_MODEL, status="incomplete", output_tokens=4000)]}))
    r = await ask_json(client, session_id, QUESTION, client_message_id="c-empty")
    assert r.status_code == 502, r.text
    assert r.json()["error"]["code"] == "UPSTREAM_AI_ERROR"
    failed = await _last_answer(client, session_id)
    assert failed["status"] == "failed" and failed["content"] == ""
    rows = await _answer_usage_rows(db, failed["id"])
    assert [(u["model"], u["output_tokens"], u["estimated"]) for u in rows] == [
        (settings.ANSWER_MODEL, 4000, False), (settings.FAST_MODEL, 4000, False)]       # both were billed

    set_provider(FakeProvider())
    retry = (await ask_json(client, session_id, QUESTION, client_message_id="c-empty")).json()
    assert retry["answer"]["status"] == "complete" and retry["answer"]["content"]
    assert retry["answer"]["id"] != failed["id"] and retry["user_message"]["id"] == failed["reply_to_id"]


def _wrap_final_commit(monkeypatch: pytest.MonkeyPatch,
                       wrap: Callable[[Callable[[], Awaitable[None]]], Awaitable[None]]) -> None:
    """Route the commit of the save that stores the complete answer through wrap(real_commit). That save opens the
    first DB session after the citations are checked."""
    armed: list[bool] = []
    real_citations_event, real_session = svc._citations_event, svc.async_session

    def citations_event(*args: Any) -> Any:
        armed.append(True)
        return real_citations_event(*args)

    def session() -> AsyncSession:
        db_session = real_session()
        if armed:
            armed.clear()
            db_session.commit = partial(wrap, db_session.commit)      # type: ignore[method-assign]
        return db_session

    monkeypatch.setattr(svc, "_citations_event", citations_event)
    monkeypatch.setattr(svc, "async_session", session)


async def _saved_turn(db: AsyncSession, message_id: uuid.UUID | None) -> tuple[dict[str, Any], list[str]]:
    msg = (await _rows(db, "SELECT status, confidence, citations FROM chat_messages WHERE id = :id", id=message_id))[0]
    usage = await _rows(db, "SELECT operation FROM ai_usage WHERE message_id = :id ORDER BY id", id=message_id)
    return dict(msg), [u["operation"] for u in usage]


async def test_disconnect_at_the_citations_event_keeps_the_answer_complete(
    client: AsyncClient, session_id: str, demo_user_id: uuid.UUID, db: AsyncSession
) -> None:
    """Regression: the answer was saved only after the `citations` event was sent, so a client leaving right then
    left a fully streamed answer stored as 'interrupted'."""
    turn = await _turn(client, session_id, demo_user_id)
    events = svc.answer(turn)
    async for event in events:
        if isinstance(event, CitationsEvent):
            break
    await events.aclose()                       # what the SSE layer does when the client goes away
    msg, operations = await _saved_turn(db, turn.assistant_message_id)
    assert msg["status"] == "complete" and msg["confidence"] and msg["citations"]
    assert operations == ["embed_query", "answer"]


async def test_cancel_right_after_the_final_commit_does_not_save_the_turn_twice(
    client: AsyncClient, session_id: str, demo_user_id: uuid.UUID, db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: the turn counted as saved only once the DB session had closed. A cancel landing in between (the
    commit already done) let answer()'s cleanup save it again as 'interrupted', with its ai_usage rows a second time."""
    async def commit_then_cancel(commit: Callable[[], Awaitable[None]]) -> None:
        await commit()
        asyncio.current_task().cancel()         # type: ignore[union-attr]  # delivered at the next await

    turn = await _turn(client, session_id, demo_user_id)
    _wrap_final_commit(monkeypatch, commit_then_cancel)

    async def drain() -> None:
        async for _ in svc.answer(turn):
            pass

    with pytest.raises(asyncio.CancelledError):
        await asyncio.create_task(drain())
    msg, operations = await _saved_turn(db, turn.assistant_message_id)
    assert msg["status"] == "complete" and msg["confidence"] and msg["citations"]
    assert operations == ["embed_query", "answer"]


def _wrap_failed_save_commit(monkeypatch: pytest.MonkeyPatch,
                             wrap: Callable[[Callable[[], Awaitable[None]]], Awaitable[None]]) -> None:
    """Route the commit of the save that stores a failed answer through wrap(real_commit). That save opens the first
    DB session after _fail() is called."""
    armed: list[bool] = []
    real_fail, real_session = svc._fail, svc.async_session

    async def fail(*args: Any, **kwargs: Any) -> Any:
        armed.append(True)
        return await real_fail(*args, **kwargs)

    def session() -> AsyncSession:
        db_session = real_session()
        if armed:
            armed.clear()
            db_session.commit = partial(wrap, db_session.commit)      # type: ignore[method-assign]
        return db_session

    monkeypatch.setattr(svc, "_fail", fail)
    monkeypatch.setattr(svc, "async_session", session)


async def test_cancel_while_the_failed_save_commits_does_not_save_the_turn_twice(
    client: AsyncClient, session_id: str, demo_user_id: uuid.UUID, db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: only the complete save was shielded. A disconnect or the JSON deadline landing while a failed
    answer's COMMIT was in flight (done on the server, its reply not back yet) let answer()'s cleanup save the turn
    again as 'interrupted', inserting its answer ai_usage row a second time."""
    error = ProviderError("transient", "OpenAI server error (500).", status_code=500)
    set_provider(PerModelStreamProvider({settings.ANSWER_MODEL: [error], settings.FAST_MODEL: [error]}))
    turn = await _turn(client, session_id, demo_user_id)
    with anyio.CancelScope() as scope:
        async def commit_then_cancel(commit: Callable[[], Awaitable[None]]) -> None:
            await commit()
            scope.cancel()              # the client leaves now, while the commit's reply is on its way back ...
            await anyio.lowlevel.checkpoint()   # ... and the cancellation is delivered inside the commit

        _wrap_failed_save_commit(monkeypatch, commit_then_cancel)
        async for _ in svc.answer(turn):
            pass
    msg, operations = await _saved_turn(db, turn.assistant_message_id)
    assert msg["status"] == "failed"
    assert operations == ["embed_query", "answer", "answer"]
    assert [r["model"] for r in await _answer_usage_rows(db, turn.assistant_message_id)] == [
        settings.ANSWER_MODEL, settings.FAST_MODEL]


async def test_json_deadline_during_the_final_save_waits_for_it(
    client: AsyncClient, session_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The complete save is shielded: the JSON deadline (like a disconnect) expiring mid-commit lets it finish.
    Regression: the commit was cut, the cleanup stored the answer as 'interrupted' and the client got a 504."""
    async def slow_commit(commit: Callable[[], Awaitable[None]]) -> None:
        await asyncio.sleep(2.5)
        await commit()

    monkeypatch.setattr(settings, "JSON_ANSWER_TIMEOUT_S", 2.0)
    _wrap_final_commit(monkeypatch, slow_commit)
    r = await ask_json(client, session_id, QUESTION)
    assert r.status_code == 200, r.text
    assert r.json()["answer"]["status"] == "complete" and r.json()["answer"]["citations"]


async def test_json_deadline_during_followups_returns_the_saved_answer(
    client: AsyncClient, session_id: str, monkeypatch: pytest.MonkeyPatch, db: AsyncSession
) -> None:
    """Regression: the JSON deadline also covered the best-effort follow-ups, so a slow follow-ups call turned an
    answer already saved 'complete' into a 504. It is returned without chips instead."""
    monkeypatch.setattr(settings, "JSON_ANSWER_TIMEOUT_S", 3.0)        # FOLLOWUPS_TIMEOUT_S (8 s) is longer
    set_provider(HangingJsonProvider("FollowupQuestions"))
    started = time.perf_counter()
    r = await ask_json(client, session_id, QUESTION)
    assert r.status_code == 200, r.text
    assert time.perf_counter() - started < 6
    answer = r.json()["answer"]
    assert answer["status"] == "complete" and answer["citations"] and answer["followups"] == []
    rows = await _rows(db, "SELECT estimated, success FROM ai_usage WHERE operation = 'followups'")
    assert rows == [{"estimated": True, "success": False}]             # the abandoned call is still on the ledger


async def test_slow_followups_are_cut_off_at_their_own_budget(
    client: AsyncClient, session_id: str, monkeypatch: pytest.MonkeyPatch, db: AsyncSession
) -> None:
    """FOLLOWUPS_TIMEOUT_S bounds the chips: the stream still ends with `done`, just without a followups event."""
    monkeypatch.setattr(settings, "FOLLOWUPS_TIMEOUT_S", 0.3)
    set_provider(HangingJsonProvider("FollowupQuestions"))             # would hang for 30 s
    started = time.perf_counter()
    events = parse_sse((await ask_stream(client, session_id, QUESTION)).text)
    assert time.perf_counter() - started < 5
    assert _names(events) == ["meta", *["token"] * _names(events).count("token"), "citations", "done"]
    assert (await _last_answer(client, session_id))["status"] == "complete"
    rows = await _rows(db, "SELECT success, error_code FROM ai_usage WHERE operation = 'followups'")
    assert rows == [{"success": False, "error_code": "UPSTREAM_TIMEOUT"}]
