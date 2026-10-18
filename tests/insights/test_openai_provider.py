"""OpenAIProvider error handling, offline: the real SDK runs against an in-process mock HTTP transport (no network,
no key). Two rules are pinned here:
  - only OpenAI's own errors become ProviderError; Celery's SoftTimeLimitExceeded keeps its type, so the worker can
    fail the run as PROCESSING_TIMEOUT instead of retrying it as a "transient" provider error;
  - a structured reply that was billed but is unusable (truncated, unparsable, refused) carries its usage, so the
    caller can still write the ai_usage row.
Also: embeddings come back at the size the database stores, and a streamed refusal is an error, not an empty answer."""

import json
from collections.abc import Callable
from typing import Any

import httpx2
import pytest
from celery.exceptions import SoftTimeLimitExceeded
from pydantic import BaseModel

from app.ai.openai_provider import OpenAIProvider
from app.ai.provider import ProviderError, StreamEnd
from app.config import settings

USAGE = {"input_tokens": 100, "input_tokens_details": {"cached_tokens": 5}, "output_tokens": 8000,
         "output_tokens_details": {"reasoning_tokens": 7000}, "total_tokens": 8100}


class Answer(BaseModel):
    answer: str


def response_body(text: str | None, status: str = "completed", refusal: str | None = None) -> dict[str, Any]:
    content = ([{"type": "refusal", "refusal": refusal}] if refusal
               else [{"type": "output_text", "text": text, "annotations": []}])
    return {"id": "resp_1", "object": "response", "created_at": 0, "model": "gpt-test", "status": status,
            "output": [{"type": "message", "id": "msg_1", "role": "assistant", "status": status,
                        "content": content}],
            "usage": USAGE}


Handler = Callable[[httpx2.Request], httpx2.Response]


@pytest.fixture
def provider(monkeypatch: pytest.MonkeyPatch) -> Callable[[Handler], OpenAIProvider]:
    monkeypatch.setattr(settings, "OPENAI_API_KEY", "sk-test-not-a-real-key")

    def make(handler: Handler) -> OpenAIProvider:
        p = OpenAIProvider(for_worker=True)

        async def async_handler(request: httpx2.Request) -> httpx2.Response:
            return handler(request)

        p._sync = p._sync.with_options(http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))
        p._async = p._async.with_options(
            http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(async_handler)))
        return p

    return make


def reply(body: dict[str, Any], status: int = 200, headers: dict[str, str] | None = None) -> Handler:
    return lambda request: httpx2.Response(status, json=body, headers=headers)


def complete(p: OpenAIProvider) -> tuple[Answer, Any]:
    return p.complete_json(model="gpt-test", effort="low", instructions="i", input="x", schema=Answer,
                           max_output_tokens=8000)


def test_valid_reply_is_parsed_with_usage(provider) -> None:
    parsed, usage = complete(provider(reply(response_body('{"answer": "60 days"}'))))
    assert parsed == Answer(answer="60 days")
    assert (usage.input_tokens, usage.cached_input_tokens, usage.output_tokens, usage.reasoning_tokens) == \
        (100, 5, 8000, 7000)


def test_reply_cut_off_mid_json_is_incomplete_with_usage(provider) -> None:
    # The SDK's own schema validation fails on the truncated JSON; before, that surfaced as a "transient" error
    # (Celery retried the same 8k-token call 6 times) and the billed tokens were lost.
    with pytest.raises(ProviderError) as exc_info:
        complete(provider(reply(response_body('{"answer": "60 da', status="incomplete"))))
    assert exc_info.value.kind == "incomplete"
    assert exc_info.value.usage is not None and exc_info.value.usage.output_tokens == 8000


def test_reply_that_does_not_fit_the_schema_is_unparsable_with_usage(provider) -> None:
    with pytest.raises(ProviderError) as exc_info:
        complete(provider(reply(response_body('{"something_else": 1}'))))
    err = exc_info.value
    assert err.kind == "bad_request" and err.status_code is None    # None: not an HTTP rejection, retry once
    assert err.usage is not None and err.usage.input_tokens == 100


def test_refusal_carries_usage(provider) -> None:
    with pytest.raises(ProviderError) as exc_info:
        complete(provider(reply(response_body(None, refusal="I can't help with that."))))
    assert exc_info.value.kind == "refusal" and exc_info.value.usage is not None


def test_http_errors_are_translated_without_usage(provider) -> None:
    p = provider(reply({"error": {"message": "slow down"}}, status=429, headers={"retry-after": "7"}))
    with pytest.raises(ProviderError) as exc_info:
        complete(p)
    assert exc_info.value.kind == "transient" and exc_info.value.retry_after == 7
    assert exc_info.value.usage is None


async def test_async_structured_output_has_the_same_rules(provider) -> None:
    p = provider(reply(response_body('{"answer": "60 da', status="incomplete")))
    with pytest.raises(ProviderError) as exc_info:
        await p.acomplete_json(model="gpt-test", effort="low", instructions="i", input="x", schema=Answer,
                               max_output_tokens=8000)
    assert exc_info.value.kind == "incomplete" and exc_info.value.usage is not None


def _soft_limit_fires(request: httpx2.Request) -> httpx2.Response:
    raise SoftTimeLimitExceeded()   # what Celery's signal handler raises while the SDK waits on the socket


def test_soft_time_limit_inside_an_embedding_call_keeps_its_type(provider) -> None:
    with pytest.raises(SoftTimeLimitExceeded):
        provider(_soft_limit_fires).embed(["text"])


def test_soft_time_limit_inside_a_structured_call_keeps_its_type(provider) -> None:
    with pytest.raises(SoftTimeLimitExceeded):
        complete(provider(_soft_limit_fires))


# --- embeddings: the size the database stores -------------------------------------------------------------------------
def embedding_body(dims: int, n: int = 1) -> dict[str, Any]:
    return {"object": "list", "model": settings.EMBED_MODEL,
            "data": [{"object": "embedding", "index": i, "embedding": [0.01] * dims} for i in range(n)],
            "usage": {"prompt_tokens": 7, "total_tokens": 7}}


def recording(body: dict[str, Any], sent: list[dict[str, Any]]) -> Handler:
    def handler(request: httpx2.Request) -> httpx2.Response:
        sent.append(json.loads(request.content))
        return httpx2.Response(200, json=body)
    return handler


def test_embeddings_ask_for_the_stored_dimensions(provider) -> None:
    sent: list[dict[str, Any]] = []
    result = provider(recording(embedding_body(settings.EMBED_DIMS, n=2), sent)).embed(["a", "b"])
    assert sent[0]["model"] == settings.EMBED_MODEL and sent[0]["dimensions"] == settings.EMBED_DIMS
    assert [len(v) for v in result.vectors] == [settings.EMBED_DIMS] * 2


def test_older_embedding_models_get_no_dimensions_parameter(provider, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "EMBED_MODEL", "text-embedding-ada-002")     # rejects `dimensions`
    sent: list[dict[str, Any]] = []
    provider(recording(embedding_body(settings.EMBED_DIMS), sent)).embed(["a"])
    assert "dimensions" not in sent[0]


async def test_embedding_of_the_wrong_size_is_rejected_with_usage(provider) -> None:
    """A 3072-d vector would be billed and then fail at the vector(1536) write; fail here, with the billed usage."""
    p = provider(reply(embedding_body(3072)))
    with pytest.raises(ProviderError) as sync_error:
        p.embed(["a"])
    with pytest.raises(ProviderError) as async_error:
        await p.aembed(["a"])
    for err in (sync_error.value, async_error.value):
        assert err.kind == "bad_request" and err.usage is not None and err.usage.input_tokens == 7


# --- streaming --------------------------------------------------------------------------------------------------------
def sse(*events: dict[str, Any]) -> Handler:
    body = "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events)
    return lambda request: httpx2.Response(200, content=body.encode(), headers={"content-type": "text/event-stream"})


def part_event(kind: str, seq: int, **fields: Any) -> dict[str, Any]:
    return {"type": kind, "item_id": "msg_1", "output_index": 0, "content_index": 0, "sequence_number": seq,
            **fields}


async def stream_items(p: OpenAIProvider) -> list[Any]:
    return [item async for item in p.astream_text(model="gpt-test", effort="low", instructions="i", input="x",
                                                  max_output_tokens=8000)]


async def test_a_streamed_answer_yields_text_then_its_end(provider) -> None:
    items = await stream_items(provider(sse(
        part_event("response.output_text.delta", 1, delta="60 days", logprobs=[]),
        {"type": "response.completed", "sequence_number": 2, "response": response_body("60 days")},
    )))
    assert items[0] == "60 days" and isinstance(items[-1], StreamEnd) and items[-1].usage.output_tokens == 8000


async def test_a_streamed_refusal_raises_instead_of_ending_empty(provider) -> None:
    """Refusal events used to be skipped: the caller saw a "successful" stream with no text at all."""
    refusal = "I can't help with that."
    with pytest.raises(ProviderError) as exc_info:
        await stream_items(provider(sse(
            part_event("response.refusal.delta", 1, delta="I can't "),
            part_event("response.refusal.delta", 2, delta="help with that."),
            part_event("response.refusal.done", 3, refusal=refusal),
            {"type": "response.completed", "sequence_number": 4, "response": response_body(None, refusal=refusal)},
        )))
    err = exc_info.value
    assert err.kind == "refusal" and err.message == refusal
    assert err.usage is not None and err.usage.output_tokens == 8000         # a refusal is billed too
