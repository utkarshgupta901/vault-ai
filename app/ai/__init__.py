"""Provider factory. The API and workers each get one provider instance per process.
Tests swap in a FakeProvider with set_provider().

In the API process the provider is wrapped in ProviderWatchdog. The cached client is reused for the life of the
process, so if it ever gets into a bad state every chat turn fails until a restart. The watchdog counts consecutive
failed calls; after PROVIDER_RESET_AFTER_FAILURES it drops the cached provider, so the next call builds a fresh client
(new connection pool). One success resets the count. The count also feeds /health/ready, which now reports the AI
check as failing instead of "ok because a key is set".

Honest scope: on 2026-10-13 every OpenAI call from one API process timed out while a new process worked. The cause was
the exhausted anyio thread pool (see api/health.py): the TLS handshake of a new connection needs a worker thread. A
fresh client would not have fixed that; the readiness/publish fixes do. The watchdog is defense in depth for a client
that is itself stuck, and the reason a recurrence shows up in /health/ready.

Workers keep the bare provider: there Celery owns failures (retries with backoff) and no user request is waiting."""

import asyncio
from collections.abc import AsyncIterator, Awaitable
from contextlib import aclosing
from typing import Any, TypeVar

import structlog

from app.ai.provider import EmbedResult, LLMProvider, Msg, ProviderError, StreamEnd, T, Usage
from app.config import settings

log = structlog.get_logger(__name__)

PROVIDER_RESET_AFTER_FAILURES = 3   # one failed chat turn = embed + answer + fallback answer = 3 failed calls
R = TypeVar("R")

_provider: LLMProvider | None = None
_consecutive_failures = 0


def get_provider(*, for_worker: bool = False) -> LLMProvider:
    global _provider
    if _provider is None:
        provider: LLMProvider
        if settings.LLM_PROVIDER == "fake":
            from app.ai.fake_provider import FakeProvider

            provider = FakeProvider()
        else:
            from app.ai.openai_provider import OpenAIProvider

            provider = OpenAIProvider(for_worker=for_worker)
        _provider = provider if for_worker else ProviderWatchdog(provider)
    return _provider


def set_provider(provider: LLMProvider | None) -> None:
    """Replace (or with None: forget) the cached provider. Starts a clean failure count."""
    global _provider, _consecutive_failures
    _provider = provider
    _consecutive_failures = 0


def consecutive_provider_failures() -> int:
    """Failed AI calls in a row in this process (0 after any success). Read by /health/ready."""
    return _consecutive_failures


class ProviderWatchdog:
    """Delegates every call to the real provider and records whether the provider answered.

    A call FAILED when it raised a transient ProviderError (timeout, 5xx, connection error, 429) or was cancelled
    before the provider gave anything back (the caller's asyncio.timeout fired). Any answer, including a refusal or a
    bad_request, means the client works, so it counts as a success. For a stream, the first item decides.
    A cancellation can also be a client that went away; counting it is harmless because one success resets the
    count, and a needless reset only costs a new HTTPS connection."""

    def __init__(self, inner: LLMProvider):
        self.inner = inner
        self.name = inner.name

    # --- outcome bookkeeping --------------------------------------------------------------------------------
    def _succeeded(self) -> None:
        global _consecutive_failures
        _consecutive_failures = 0

    def _failed(self, reason: str) -> None:
        global _consecutive_failures, _provider
        _consecutive_failures += 1
        if _consecutive_failures % PROVIDER_RESET_AFTER_FAILURES == 0 and _provider is self:
            _provider = None   # the next get_provider() builds a fresh client; in-flight calls keep this one
            log.warning("ai_provider_reset", consecutive_failures=_consecutive_failures, reason=reason)

    def _record_error(self, exc: ProviderError) -> None:
        if exc.kind == "transient":
            self._failed(exc.message)
        else:
            self._succeeded()

    async def _watch(self, call: Awaitable[R]) -> R:
        try:
            result = await call
        except ProviderError as exc:
            self._record_error(exc)
            raise
        except asyncio.CancelledError:
            self._failed("cancelled before the provider answered (timeout)")
            raise
        self._succeeded()
        return result

    # --- LLMProvider -----------------------------------------------------------------------------------------
    def embed(self, texts: list[str]) -> EmbedResult:
        return self.inner.embed(texts)

    def complete_json(self, *, model: str, effort: str, instructions: str, input: str | list[Msg],
                      schema: type[T], max_output_tokens: int) -> tuple[T, Usage]:
        return self.inner.complete_json(model=model, effort=effort, instructions=instructions, input=input,
                                        schema=schema, max_output_tokens=max_output_tokens)

    async def aembed(self, texts: list[str]) -> EmbedResult:
        return await self._watch(self.inner.aembed(texts))

    async def acomplete_json(self, *, model: str, effort: str, instructions: str, input: str | list[Msg],
                             schema: type[T], max_output_tokens: int) -> tuple[T, Usage]:
        return await self._watch(self.inner.acomplete_json(
            model=model, effort=effort, instructions=instructions, input=input, schema=schema,
            max_output_tokens=max_output_tokens))

    async def acomplete_text(self, *, model: str, effort: str, instructions: str, input: str | list[Msg],
                             max_output_tokens: int) -> tuple[str, Usage]:
        return await self._watch(self.inner.acomplete_text(
            model=model, effort=effort, instructions=instructions, input=input, max_output_tokens=max_output_tokens))

    async def astream_text(self, *, model: str, effort: str, instructions: str, input: str | list[Msg],
                           max_output_tokens: int) -> AsyncIterator[str | StreamEnd]:
        stream: Any = self.inner.astream_text(model=model, effort=effort, instructions=instructions, input=input,
                                              max_output_tokens=max_output_tokens)
        answered = False
        async with aclosing(stream):   # closing us closes the provider's HTTP stream too
            try:
                async for item in stream:
                    if not answered:
                        answered = True
                        self._succeeded()
                    yield item
            except ProviderError as exc:
                if not answered:
                    self._record_error(exc)
                raise
            except asyncio.CancelledError:
                if not answered:
                    self._failed("cancelled before the first token (timeout)")
                raise
