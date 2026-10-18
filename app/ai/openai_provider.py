"""OpenAI implementation of LLMProvider (Responses API + Embeddings API). The only module that imports `openai`.

Retries: the API process uses max_retries=1 (a user is waiting; per-step timeouts sit on top). Workers use
max_retries=0 because Celery owns retries there (with backoff + Retry-After) — two retry layers would multiply.

Errors: only the SDK's own errors (openai.OpenAIError) are translated into ProviderError, plus a reply that cannot be
parsed into the schema. Everything else keeps its type. In particular Celery's SoftTimeLimitExceeded is an ordinary
Exception raised wherever the worker happens to be, usually inside an HTTP call to OpenAI; it must reach
worker/attempt.py to become PROCESSING_TIMEOUT (wrapped as a "transient" ProviderError it was retried 6 more times,
each with a fresh time limit). asyncio cancellation is a BaseException and was never caught."""

import json
import time
from collections.abc import AsyncIterator
from typing import Any

import openai
import pydantic
from openai import AsyncOpenAI, OpenAI

from app.ai.provider import EmbedResult, Msg, ProviderError, StreamEnd, T, Usage
from app.config import settings


def _retry_after(exc: openai.APIStatusError) -> float | None:
    try:
        value = exc.response.headers.get("retry-after")
        return float(value) if value else None
    except (ValueError, AttributeError):
        return None


# The SDK validates a structured reply against the schema inside parse(): truncated JSON, or JSON that does not fit
# the schema, surfaces as one of these (not as an OpenAIError).
_PARSE_ERRORS = (pydantic.ValidationError, json.JSONDecodeError)


def _translate(exc: openai.OpenAIError) -> ProviderError:
    if isinstance(exc, openai.RateLimitError):
        return ProviderError("transient", "Rate limited by OpenAI.", _retry_after(exc), 429)
    if isinstance(exc, openai.APITimeoutError):
        return ProviderError("transient", "OpenAI request timed out.")
    if isinstance(exc, openai.APIConnectionError):
        return ProviderError("transient", "Could not reach OpenAI.")
    if isinstance(exc, openai.InternalServerError):
        return ProviderError("transient", f"OpenAI server error ({exc.status_code}).", None, exc.status_code)
    if isinstance(exc, openai.APIStatusError):
        if exc.status_code in (408, 409, 502, 503, 504):
            return ProviderError("transient", f"OpenAI unavailable ({exc.status_code}).", _retry_after(exc), exc.status_code)
        return ProviderError("bad_request", f"OpenAI rejected the request ({exc.status_code}): {exc.message}", None, exc.status_code)
    return ProviderError("transient", f"Unexpected OpenAI error: {exc}")


def _get(obj: Any, name: str) -> Any:
    """A field of an SDK object, or of the raw JSON body (a dict) of the same response; None when absent."""
    return obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)


def _usage(model: str, response: Any, started: float) -> Usage:
    u = _get(response, "usage")
    return Usage(
        model=model,
        input_tokens=_get(u, "input_tokens") or 0,
        cached_input_tokens=_get(_get(u, "input_tokens_details"), "cached_tokens") or 0,
        output_tokens=_get(u, "output_tokens") or 0,
        reasoning_tokens=_get(_get(u, "output_tokens_details"), "reasoning_tokens") or 0,
        latency_ms=int((time.perf_counter() - started) * 1000),
    )


def _check_parsed(response: Any, usage: Usage) -> Any:
    """Turn the different 'no usable structured output' cases into typed errors. Each carries `usage`: the call was
    billed even though its answer is unusable."""
    if getattr(response, "status", None) == "incomplete":
        raise ProviderError("incomplete", "Model output was truncated (max_output_tokens).", usage=usage)
    parsed = getattr(response, "output_parsed", None)
    if parsed is None:
        for item in getattr(response, "output", None) or []:
            for part in getattr(item, "content", None) or []:
                if getattr(part, "type", None) == "refusal":
                    raise ProviderError("refusal", getattr(part, "refusal", "Model refused."), usage=usage)
        raise ProviderError("bad_request", "Model returned no parsable output.", usage=usage)
    return parsed


def _raw_body(raw: Any) -> dict[str, Any]:
    try:
        body = raw.http_response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _parse_structured(raw: Any, model: str, started: float) -> tuple[Any, Usage]:
    """Parse a structured-output reply that was requested with `with_raw_response`.

    Why raw: the SDK validates the JSON against the schema inside parse(). When the output was cut off mid-JSON (or
    does not fit the schema) that raises before any response object exists, which would lose the status and the
    usage of a call OpenAI has billed. Reading them from the raw body keeps both: truncated -> "incomplete" (the
    caller retries once with a bigger budget), otherwise "no parsable output"; either way with usage attached."""
    try:
        response = raw.parse()
    except _PARSE_ERRORS as exc:
        body = _raw_body(raw)
        usage = _usage(model, body, started)
        if body.get("status") == "incomplete":
            raise ProviderError("incomplete", "Model output was truncated (max_output_tokens).", usage=usage) from exc
        raise ProviderError("bad_request", "Model returned no parsable output.", usage=usage) from exc
    usage = _usage(model, response, started)
    return _check_parsed(response, usage), usage


def _reasoning(effort: str) -> dict[str, str]:
    return {"effort": effort}


def _embed_params() -> dict[str, Any]:
    """text-embedding-3 models return 1536 (small) or 3072 (large) dimensions unless told otherwise, and the chunks
    table stores vector(EMBED_DIMS); asking for EMBED_DIMS keeps a model switch from failing at the write. Older
    models (ada-002) reject the parameter."""
    if settings.EMBED_MODEL.startswith("text-embedding-3"):
        return {"model": settings.EMBED_MODEL, "dimensions": settings.EMBED_DIMS}
    return {"model": settings.EMBED_MODEL}


class OpenAIProvider:
    name = "openai"

    def __init__(self, *, for_worker: bool = False):
        retries = 0 if for_worker else settings.API_LLM_MAX_RETRIES
        timeout = settings.WORKER_LLM_TIMEOUT_S if for_worker else settings.API_LLM_TIMEOUT_S
        self._sync = OpenAI(api_key=settings.OPENAI_API_KEY, max_retries=retries, timeout=timeout)
        self._async = AsyncOpenAI(api_key=settings.OPENAI_API_KEY, max_retries=retries, timeout=timeout)

    # --- Embeddings ------------------------------------------------------------------------------------
    def embed(self, texts: list[str]) -> EmbedResult:
        started = time.perf_counter()
        try:
            r = self._sync.embeddings.create(input=texts, **_embed_params())
        except openai.OpenAIError as exc:  # only OpenAI's errors: see the module docstring
            raise _translate(exc) from exc
        return self._embed_result(r, started)

    async def aembed(self, texts: list[str]) -> EmbedResult:
        started = time.perf_counter()
        try:
            r = await self._async.embeddings.create(input=texts, **_embed_params())
        except openai.OpenAIError as exc:
            raise _translate(exc) from exc
        return self._embed_result(r, started)

    @staticmethod
    def _embed_result(r: Any, started: float) -> EmbedResult:
        vectors = [d.embedding for d in sorted(r.data, key=lambda d: d.index)]
        usage = Usage(
            model=settings.EMBED_MODEL,
            input_tokens=r.usage.prompt_tokens,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )
        wrong = {len(v) for v in vectors} - {settings.EMBED_DIMS}
        if wrong:   # would fail later at the vector(EMBED_DIMS) write, after the call was billed
            raise ProviderError("bad_request", f"Embedding model returned {sorted(wrong)} dimensions, expected "
                                f"{settings.EMBED_DIMS}.", usage=usage)
        return EmbedResult(vectors=vectors, usage=usage)

    # --- Structured output -------------------------------------------------------------------------------
    def complete_json(self, *, model: str, effort: str, instructions: str, input: str | list[Msg],
                      schema: type[T], max_output_tokens: int) -> tuple[T, Usage]:
        started = time.perf_counter()
        try:
            raw = self._sync.responses.with_raw_response.parse(
                model=model, reasoning=_reasoning(effort), instructions=instructions, input=input,
                text_format=schema, max_output_tokens=max_output_tokens,
            )
        except openai.OpenAIError as exc:
            raise _translate(exc) from exc
        return _parse_structured(raw, model, started)

    async def acomplete_json(self, *, model: str, effort: str, instructions: str, input: str | list[Msg],
                             schema: type[T], max_output_tokens: int) -> tuple[T, Usage]:
        started = time.perf_counter()
        try:
            raw = await self._async.responses.with_raw_response.parse(
                model=model, reasoning=_reasoning(effort), instructions=instructions, input=input,
                text_format=schema, max_output_tokens=max_output_tokens,
            )
        except openai.OpenAIError as exc:
            raise _translate(exc) from exc
        return _parse_structured(raw, model, started)

    # --- Free text -------------------------------------------------------------------------------------
    async def acomplete_text(self, *, model: str, effort: str, instructions: str, input: str | list[Msg],
                             max_output_tokens: int) -> tuple[str, Usage]:
        started = time.perf_counter()
        try:
            r = await self._async.responses.create(
                model=model, reasoning=_reasoning(effort), instructions=instructions, input=input,
                max_output_tokens=max_output_tokens,
            )
        except openai.OpenAIError as exc:
            raise _translate(exc) from exc
        if r.status == "incomplete" and not r.output_text:
            raise ProviderError("incomplete", "Model output was truncated (max_output_tokens).",
                                usage=_usage(model, r, started))
        return r.output_text, _usage(model, r, started)

    async def astream_text(self, *, model: str, effort: str, instructions: str, input: str | list[Msg],
                           max_output_tokens: int) -> AsyncIterator[str | StreamEnd]:
        started = time.perf_counter()
        try:
            stream = await self._async.responses.create(
                model=model, reasoning=_reasoning(effort), instructions=instructions, input=input,
                max_output_tokens=max_output_tokens, stream=True,
            )
        except openai.OpenAIError as exc:
            raise _translate(exc) from exc
        # A refusal arrives as refusal events, not text. Skipping them gave the caller an empty "successful" stream;
        # instead it becomes the same "refusal" error as in structured output, raised once the usage is known.
        answered = False
        refusal: list[str] = []
        try:
            async for event in stream:
                if event.type == "response.output_text.delta":
                    answered = True
                    yield event.delta
                elif event.type == "response.refusal.delta":
                    refusal.append(event.delta)
                elif event.type in ("response.completed", "response.incomplete"):
                    usage = _usage(model, event.response, started)
                    if refusal and not answered:
                        raise ProviderError("refusal", "".join(refusal) or "Model refused.", usage=usage)
                    yield StreamEnd(usage=usage, status=event.response.status or "completed")
                    return
                elif event.type in ("response.failed", "error"):
                    raise ProviderError("transient", f"OpenAI stream failed: {getattr(event, 'message', event.type)}")
            if refusal and not answered:     # the stream ended without its completion event
                raise ProviderError("refusal", "".join(refusal) or "Model refused.")
        except openai.OpenAIError as exc:
            raise _translate(exc) from exc
        except _PARSE_ERRORS as exc:  # a garbled event: a provider/network glitch, worth a retry like any other
            raise ProviderError("transient", "OpenAI stream sent an unreadable event.") from exc
        finally:
            await stream.close()
