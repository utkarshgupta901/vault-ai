"""OpenAIProvider error handling, offline: the real SDK runs against an in-process mock HTTP transport (no network,
no key). Two rules are pinned here:
  - only OpenAI's own errors become ProviderError; Celery's SoftTimeLimitExceeded keeps its type, so the worker can
    fail the run as PROCESSING_TIMEOUT instead of retrying it as a "transient" provider error;
  - a structured reply that was billed but is unusable (truncated, unparsable, refused) carries its usage, so the
    caller can still write the ai_usage row."""

from collections.abc import Callable
from typing import Any

import httpx2
import pytest
from celery.exceptions import SoftTimeLimitExceeded
from pydantic import BaseModel

from app.ai.openai_provider import OpenAIProvider
from app.ai.provider import ProviderError
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
