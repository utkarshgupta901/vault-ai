"""The API's provider watchdog: after PROVIDER_RESET_AFTER_FAILURES failed calls in a row the cached provider is
dropped, so the next get_provider() builds a fresh client; any answer resets the count."""

import asyncio
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest

import app.ai as ai
from app.ai import PROVIDER_RESET_AFTER_FAILURES, ProviderWatchdog, get_provider, set_provider
from app.ai.fake_provider import FakeProvider
from app.ai.provider import ProviderError, StreamEnd

TRANSIENT = ProviderError("transient", "OpenAI request timed out.")
STREAM_ARGS: dict[str, Any] = {"model": "m", "effort": "low", "instructions": "", "input": "q",
                               "max_output_tokens": 10}


@pytest.fixture
def watched() -> Iterator[ProviderWatchdog]:
    set_provider(None)
    provider = get_provider()                    # the API path: built and wrapped by the factory
    assert isinstance(provider, ProviderWatchdog) and isinstance(provider.inner, FakeProvider)
    yield provider
    set_provider(None)


async def test_resets_after_consecutive_transient_failures(watched: ProviderWatchdog) -> None:
    fake: FakeProvider = watched.inner           # type: ignore[assignment]
    fake.fail_next.extend([TRANSIENT] * PROVIDER_RESET_AFTER_FAILURES)
    for i in range(PROVIDER_RESET_AFTER_FAILURES):
        assert get_provider() is watched, f"reset too early (after {i} failures)"
        with pytest.raises(ProviderError):
            await watched.aembed(["q"])
    assert ai.consecutive_provider_failures() == PROVIDER_RESET_AFTER_FAILURES
    fresh = get_provider()
    assert fresh is not watched and isinstance(fresh, ProviderWatchdog)
    await fresh.aembed(["q"])                     # the fresh client answers ...
    assert ai.consecutive_provider_failures() == 0   # ... and the count starts over


async def test_an_answer_resets_the_count(watched: ProviderWatchdog) -> None:
    fake: FakeProvider = watched.inner           # type: ignore[assignment]
    fake.fail_next.extend([TRANSIENT] * (PROVIDER_RESET_AFTER_FAILURES - 1))
    for _ in range(PROVIDER_RESET_AFTER_FAILURES - 1):
        with pytest.raises(ProviderError):
            await watched.aembed(["q"])
    await watched.aembed(["q"])
    fake.fail_next.append(TRANSIENT)
    with pytest.raises(ProviderError):
        await watched.aembed(["q"])
    assert get_provider() is watched              # 2 failures, a success, 1 failure: never 3 in a row


async def test_non_transient_errors_mean_the_client_works(watched: ProviderWatchdog) -> None:
    fake: FakeProvider = watched.inner           # type: ignore[assignment]
    fake.fail_next.extend([TRANSIENT, TRANSIENT, ProviderError("bad_request", "invalid model")])
    for _ in range(3):
        with pytest.raises(ProviderError):
            await watched.acomplete_text(**STREAM_ARGS)
    assert ai.consecutive_provider_failures() == 0 and get_provider() is watched


class Hanging(FakeProvider):
    """A provider whose calls never answer (the incident: every call ran into the caller's timeout)."""

    async def aembed(self, texts: list[str]) -> Any:
        await asyncio.sleep(10)

    async def astream_text(self, **_: Any) -> AsyncIterator[str | StreamEnd]:
        await asyncio.sleep(10)
        yield "never"


async def test_caller_timeouts_count_as_failures() -> None:
    watched = ProviderWatchdog(Hanging())
    set_provider(watched)
    try:
        for _ in range(PROVIDER_RESET_AFTER_FAILURES - 1):
            with pytest.raises(TimeoutError):
                async with asyncio.timeout(0.05):       # how rag/cache.py bounds the query embedding
                    await watched.aembed(["q"])
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.05):           # how rag/generation.py bounds the first token
                await anext(watched.astream_text(**STREAM_ARGS))
        assert get_provider() is not watched
    finally:
        set_provider(None)


async def test_stream_is_judged_by_its_first_item(watched: ProviderWatchdog) -> None:
    fake: FakeProvider = watched.inner           # type: ignore[assignment]
    fake.fail_next.append(TRANSIENT)
    with pytest.raises(ProviderError):
        async for _ in watched.astream_text(**STREAM_ARGS):
            pass
    assert ai.consecutive_provider_failures() == 1
    items = [item async for item in watched.astream_text(**STREAM_ARGS)]
    assert isinstance(items[-1], StreamEnd)       # passes every item through unchanged
    assert ai.consecutive_provider_failures() == 0


def test_workers_get_the_bare_provider() -> None:
    set_provider(None)
    try:
        assert isinstance(get_provider(for_worker=True), FakeProvider)   # Celery retries own failures there
    finally:
        set_provider(None)
