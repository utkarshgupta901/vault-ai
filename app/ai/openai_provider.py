"""OpenAI implementation of LLMProvider (Responses API + Embeddings API). The only module that imports `openai`.

Retries: the API process uses max_retries=1 (a user is waiting; per-step timeouts sit on top). Workers use
max_retries=0 because Celery owns retries there (with backoff + Retry-After) — two retry layers would multiply."""

import time
from collections.abc import AsyncIterator
from typing import Any

import openai
from openai import AsyncOpenAI, OpenAI

from app.ai.provider import EmbedResult, Msg, ProviderError, StreamEnd, T, Usage
from app.config import settings


def _retry_after(exc: openai.APIStatusError) -> float | None:
    try:
        value = exc.response.headers.get("retry-after")
        return float(value) if value else None
    except (ValueError, AttributeError):
        return None


def _translate(exc: Exception) -> ProviderError:
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


def _usage(model: str, response: Any, started: float) -> Usage:
    u = getattr(response, "usage", None)
    return Usage(
        model=model,
        input_tokens=getattr(u, "input_tokens", 0) or 0,
        cached_input_tokens=getattr(getattr(u, "input_tokens_details", None), "cached_tokens", 0) or 0,
        output_tokens=getattr(u, "output_tokens", 0) or 0,
        reasoning_tokens=getattr(getattr(u, "output_tokens_details", None), "reasoning_tokens", 0) or 0,
        latency_ms=int((time.perf_counter() - started) * 1000),
    )


def _check_parsed(response: Any) -> Any:
    """Turn the different 'no usable structured output' cases into typed errors."""
    if getattr(response, "status", None) == "incomplete":
        raise ProviderError("incomplete", "Model output was truncated (max_output_tokens).")
    parsed = getattr(response, "output_parsed", None)
    if parsed is None:
        for item in getattr(response, "output", None) or []:
            for part in getattr(item, "content", None) or []:
                if getattr(part, "type", None) == "refusal":
                    raise ProviderError("refusal", getattr(part, "refusal", "Model refused."))
        raise ProviderError("bad_request", "Model returned no parsable output.")
    return parsed


def _reasoning(effort: str) -> dict[str, str]:
    return {"effort": effort}


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
            r = self._sync.embeddings.create(model=settings.EMBED_MODEL, input=texts)
        except Exception as exc:  # noqa: BLE001 - translated to a vendor-neutral error
            raise _translate(exc) from exc
        return self._embed_result(r, started)

    async def aembed(self, texts: list[str]) -> EmbedResult:
        started = time.perf_counter()
        try:
            r = await self._async.embeddings.create(model=settings.EMBED_MODEL, input=texts)
        except Exception as exc:  # noqa: BLE001
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
        return EmbedResult(vectors=vectors, usage=usage)

    # --- Structured output -------------------------------------------------------------------------------
    def complete_json(self, *, model: str, effort: str, instructions: str, input: str | list[Msg],
                      schema: type[T], max_output_tokens: int) -> tuple[T, Usage]:
        started = time.perf_counter()
        try:
            r = self._sync.responses.parse(
                model=model, reasoning=_reasoning(effort), instructions=instructions, input=input,
                text_format=schema, max_output_tokens=max_output_tokens,
            )
        except ProviderError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise _translate(exc) from exc
        return _check_parsed(r), _usage(model, r, started)

    async def acomplete_json(self, *, model: str, effort: str, instructions: str, input: str | list[Msg],
                             schema: type[T], max_output_tokens: int) -> tuple[T, Usage]:
        started = time.perf_counter()
        try:
            r = await self._async.responses.parse(
                model=model, reasoning=_reasoning(effort), instructions=instructions, input=input,
                text_format=schema, max_output_tokens=max_output_tokens,
            )
        except Exception as exc:  # noqa: BLE001
            raise _translate(exc) from exc
        return _check_parsed(r), _usage(model, r, started)

    # --- Free text -------------------------------------------------------------------------------------
    async def acomplete_text(self, *, model: str, effort: str, instructions: str, input: str | list[Msg],
                             max_output_tokens: int) -> tuple[str, Usage]:
        started = time.perf_counter()
        try:
            r = await self._async.responses.create(
                model=model, reasoning=_reasoning(effort), instructions=instructions, input=input,
                max_output_tokens=max_output_tokens,
            )
        except Exception as exc:  # noqa: BLE001
            raise _translate(exc) from exc
        if r.status == "incomplete" and not r.output_text:
            raise ProviderError("incomplete", "Model output was truncated (max_output_tokens).")
        return r.output_text, _usage(model, r, started)

    async def astream_text(self, *, model: str, effort: str, instructions: str, input: str | list[Msg],
                           max_output_tokens: int) -> AsyncIterator[str | StreamEnd]:
        started = time.perf_counter()
        try:
            stream = await self._async.responses.create(
                model=model, reasoning=_reasoning(effort), instructions=instructions, input=input,
                max_output_tokens=max_output_tokens, stream=True,
            )
        except Exception as exc:  # noqa: BLE001
            raise _translate(exc) from exc
        try:
            async for event in stream:
                if event.type == "response.output_text.delta":
                    yield event.delta
                elif event.type in ("response.completed", "response.incomplete"):
                    yield StreamEnd(usage=_usage(model, event.response, started), status=event.response.status or "completed")
                    return
                elif event.type in ("response.failed", "error"):
                    raise ProviderError("transient", f"OpenAI stream failed: {getattr(event, 'message', event.type)}")
        except ProviderError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise _translate(exc) from exc
        finally:
            await stream.close()
