"""Query-embedding cache (app/rag/cache.py). It is optional, so every Redis failure is a miss and never an error, and
a hit must give back exactly the vector stored for this embedding model and these dimensions."""

import asyncio
import time
from array import array

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.fake_provider import FakeProvider, fake_vector
from app.config import settings
from app.core.redis import get_async_cache
from app.rag import cache
from app.rag.cache import cache_key, embed_query, get_cached_vector, set_cached_vector


class _BrokenCache:
    """Stands in for the Valkey client when it is down (raises) or hangs (sleeps past REDIS_TIMEOUT_S)."""

    def __init__(self, error: Exception | None = None, delay_s: float = 0.0) -> None:
        self.error, self.delay_s = error, delay_s

    async def _fail(self) -> None:
        await asyncio.sleep(self.delay_s)
        if self.error:
            raise self.error

    async def get(self, key: str) -> bytes | None:
        await self._fail()
        return None

    async def set(self, key: str, value: bytes, ex: int | None = None) -> None:
        await self._fail()


@pytest.mark.parametrize("broken", [_BrokenCache(error=ConnectionError("valkey down")), _BrokenCache(delay_s=5)],
                         ids=["raises", "hangs"])
async def test_redis_failure_is_a_miss_not_an_error(monkeypatch: pytest.MonkeyPatch, broken: _BrokenCache) -> None:
    monkeypatch.setattr(cache, "get_async_cache", lambda: broken)
    monkeypatch.setattr(settings, "REDIS_TIMEOUT_S", 0.05)
    started = time.perf_counter()
    assert await get_cached_vector("how many PTO days?") is None
    await set_cached_vector("how many PTO days?", [0.5] * settings.EMBED_DIMS)       # swallowed, not raised
    assert time.perf_counter() - started < 1.0                                         # bounded by the timeout

    result = await embed_query(FakeProvider(), "how many PTO days?")     # the answer path still gets its vector
    assert result.vector == fake_vector("how many PTO days?")
    assert not result.cache_hit and result.error is None


async def test_vector_round_trips_as_float32(db: AsyncSession) -> None:
    vector = [(-1) ** i * (i + 1) / 3 for i in range(settings.EMBED_DIMS)]          # not exact in float32
    await set_cached_vector("q", vector)
    raw = await get_async_cache().get(cache_key("q"))
    assert len(raw) == 4 * settings.EMBED_DIMS                                         # packed float32, not JSON
    cached = await get_cached_vector("q")
    assert cached == array("f", vector).tolist()
    assert cached == pytest.approx(vector, rel=1e-6)


async def test_embed_query_serves_the_second_call_from_the_cache(db: AsyncSession) -> None:
    provider = FakeProvider()
    first = await embed_query(provider, "notice period under clause 9.2")
    second = await embed_query(provider, "notice period under clause 9.2")
    assert not first.cache_hit and second.cache_hit
    assert [c["op"] for c in provider.calls] == ["embed"]                             # one API call, not two
    assert second.vector == pytest.approx(first.vector, rel=1e-6)
    assert second.usage.input_tokens == 0


@pytest.mark.parametrize("raw", [
    array("f", [0.5] * (settings.EMBED_DIMS - 1)).tobytes(),      # another dimension count
    array("f", [0.5] * settings.EMBED_DIMS).tobytes()[:-1],       # truncated: not a whole number of float32s
    b"not a vector",
], ids=["other-dims", "truncated", "garbage"])
async def test_stored_value_of_the_wrong_length_is_a_miss(db: AsyncSession, raw: bytes) -> None:
    await get_async_cache().set(cache_key("q"), raw)
    assert await get_cached_vector("q") is None
    assert (await embed_query(FakeProvider(), "q")).vector == fake_vector("q")      # a miss, not keyword-only


def test_cache_key_includes_model_and_dims(monkeypatch: pytest.MonkeyPatch) -> None:
    base = cache_key("q")
    assert base.startswith("qemb:") and cache_key("q") == base and cache_key("q ") != base
    with monkeypatch.context() as m:
        m.setattr(settings, "EMBED_MODEL", "text-embedding-3-large")
        other_model = cache_key("q")
    with monkeypatch.context() as m:
        m.setattr(settings, "EMBED_DIMS", 512)
        other_dims = cache_key("q")
    assert len({base, other_model, other_dims}) == 3
    assert cache_key("q") == base
