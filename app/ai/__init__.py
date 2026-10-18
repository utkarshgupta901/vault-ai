"""Provider factory. The API and workers each get one provider instance per process.
Tests swap in a FakeProvider with set_provider().

In the API process the provider is wrapped in ProviderWatchdog. The cached client is reused for the life of the
process, so if it ever gets into a bad state every chat turn fails until a restart. The watchdog counts consecutive
failed calls per path (embeddings, text generation); after PROVIDER_RESET_AFTER_FAILURES on either path it drops the
cached provider, so the next call builds a fresh client (new connection pool). A success resets that path's count.
The counts are per path because a chat turn mixes both: a working query embedding between two failed answer calls
must not hide that no answer can be generated. The counts also feed /health/ready, which reports the AI check as
failing instead of "ok because a key is set".

The watchdog also remembers an auth/model rejection (HTTP 401/403/404) until that path succeeds again. Those calls
"answer", so they never counted as failures, yet with a revoked key or a missing model every call fails the same way
(2026-10-17: readiness said ai ok while every call returned 401). A fresh client would not help, so they do not count
towards a reset; readiness reports them instead.

Honest scope: on 2026-10-13 every OpenAI call from one API process timed out while a new process worked. The cause was
the exhausted anyio thread pool (see api/health.py): the TLS handshake of a new connection needs a worker thread. A
fresh client would not have fixed that; the readiness/publish fixes do. The watchdog is defense in depth for a client
that is itself stuck, and the reason a recurrence shows up in /health/ready.

Workers keep the bare provider: there Celery owns failures (retries with backoff) and no user request is waiting."""

import asyncio
from collections.abc import AsyncIterator, Awaitable
from contextlib import aclosing
from dataclasses import dataclass
from typing import Any, TypeVar

import structlog

from app.ai.provider import EmbedResult, LLMProvider, Msg, ProviderError, StreamEnd, T, Usage
from app.config import settings

log = structlog.get_logger(__name__)

# Per path. Text generation: one failed follow-up turn = condense + answer + fallback answer = 3 failed calls (2 on a
# first turn, which has no condense). Embeddings: one query embedding per turn, so 3 failed turns in a row.
PROVIDER_RESET_AFTER_FAILURES = 3
AUTH_REJECTED_STATUSES = (401, 403, 404)   # key rejected / no access / model (or endpoint) not found
EMBEDDINGS, GENERATION = "embeddings", "generation"
R = TypeVar("R")


@dataclass
class _PathHealth:
    """What the watchdog knows about one call path. A success replaces it with a fresh one."""

    failures: int = 0                       # transient failures (or caller timeouts) in a row
    rejected_status: int | None = None      # HTTP status of an auth/model rejection, until this path succeeds


def _fresh_health() -> dict[str, _PathHealth]:
    return {EMBEDDINGS: _PathHealth(), GENERATION: _PathHealth()}


_provider: LLMProvider | None = None
_health = _fresh_health()


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
    global _provider, _health
    _provider = provider
    _health = _fresh_health()


def provider_failures() -> dict[str, int]:
    """Failed AI calls in a row per path in this process (0 after that path's next success). Read by /health/ready."""
    return {path: h.failures for path, h in _health.items()}


def consecutive_provider_failures() -> int:
    """The worse path's count, for callers that want one number."""
    return max(provider_failures().values())


def provider_rejections() -> dict[str, int]:
    """Paths whose provider currently rejects our key or model -> that HTTP status. Read by /health/ready."""
    return {path: h.rejected_status for path, h in _health.items() if h.rejected_status is not None}


class ProviderWatchdog:
    """Delegates every call to the real provider and records whether the provider answered, per path.

    A call FAILED when it raised a transient ProviderError (timeout, 5xx, connection error, 429) or was cancelled
    before the provider gave anything back (the caller's asyncio.timeout fired). A 401/403/404 is a REJECTION: the
    client works, but our key or model does not; it is remembered until the path succeeds. Any other answer,
    including a refusal or a bad_request, means the client works, so it counts as a success. For a stream, the first
    item decides. A cancellation can also be a client that went away; counting it is harmless because one success
    resets the count, and a needless reset only costs a new HTTPS connection."""

    def __init__(self, inner: LLMProvider):
        self.inner = inner
        self.name = inner.name

    # --- outcome bookkeeping --------------------------------------------------------------------------------
    def _succeeded(self, path: str) -> None:
        _health[path] = _PathHealth()

    def _failed(self, path: str, reason: str) -> None:
        global _provider
        health = _health[path]
        health.failures += 1
        if health.failures % PROVIDER_RESET_AFTER_FAILURES == 0 and _provider is self:
            _provider = None   # the next get_provider() builds a fresh client; in-flight calls keep this one
            log.warning("ai_provider_reset", path=path, consecutive_failures=health.failures, reason=reason)

    def _rejected(self, path: str, status: int) -> None:
        """No reset (a fresh client sends the same key and model). Logged once per change, never with the vendor's
        message: it can echo part of the key."""
        if _health[path].rejected_status != status:
            log.warning("ai_provider_rejected", path=path, status_code=status)
        _health[path] = _PathHealth(rejected_status=status)

    def _record_error(self, path: str, exc: ProviderError) -> None:
        if exc.status_code is not None and exc.status_code in AUTH_REJECTED_STATUSES:
            self._rejected(path, exc.status_code)
        elif exc.kind == "transient":
            self._failed(path, exc.message)
        else:
            self._succeeded(path)

    async def _watch(self, path: str, call: Awaitable[R]) -> R:
        try:
            result = await call
        except ProviderError as exc:
            self._record_error(path, exc)
            raise
        except asyncio.CancelledError:
            self._failed(path, "cancelled before the provider answered (timeout)")
            raise
        self._succeeded(path)
        return result

    # --- LLMProvider -----------------------------------------------------------------------------------------
    def embed(self, texts: list[str]) -> EmbedResult:
        return self.inner.embed(texts)

    def complete_json(self, *, model: str, effort: str, instructions: str, input: str | list[Msg],
                      schema: type[T], max_output_tokens: int) -> tuple[T, Usage]:
        return self.inner.complete_json(model=model, effort=effort, instructions=instructions, input=input,
                                        schema=schema, max_output_tokens=max_output_tokens)

    async def aembed(self, texts: list[str]) -> EmbedResult:
        return await self._watch(EMBEDDINGS, self.inner.aembed(texts))

    async def acomplete_json(self, *, model: str, effort: str, instructions: str, input: str | list[Msg],
                             schema: type[T], max_output_tokens: int) -> tuple[T, Usage]:
        return await self._watch(GENERATION, self.inner.acomplete_json(
            model=model, effort=effort, instructions=instructions, input=input, schema=schema,
            max_output_tokens=max_output_tokens))

    async def acomplete_text(self, *, model: str, effort: str, instructions: str, input: str | list[Msg],
                             max_output_tokens: int) -> tuple[str, Usage]:
        return await self._watch(GENERATION, self.inner.acomplete_text(
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
                        self._succeeded(GENERATION)
                    yield item
            except ProviderError as exc:
                if not answered:
                    self._record_error(GENERATION, exc)
                raise
            except asyncio.CancelledError:
                if not answered:
                    self._failed(GENERATION, "cancelled before the first token (timeout)")
                raise
