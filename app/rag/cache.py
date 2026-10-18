"""Query-embedding cache in Valkey/Redis db1, plus `embed_query()` which uses it.

Why cache: the same question (a starter question, a demo script, an eval rerun, a retried request) produces the same
vector, so a cache hit saves an OpenAI round trip (~150-300 ms) on the critical path to the first token.
Key = sha256(model | dims | text): changing the embedding model or dimensions can never return a stale vector.
Value = the vector packed as float32 bytes (6 KB for 1536 dims; JSON would be ~5x bigger). TTL 24 h.

The cache is optional: every Redis call is bounded by REDIS_TIMEOUT_S (200 ms) and fails OPEN (treated as a miss),
so a Redis outage costs latency, never correctness.
"""

import asyncio
import hashlib
import time
from array import array
from dataclasses import dataclass

import structlog

from app.ai.provider import LLMProvider, Usage
from app.config import settings
from app.core.redis import get_async_cache

log = structlog.get_logger(__name__)
TTL_S = 24 * 3600


def cache_key(text: str) -> str:
    digest = hashlib.sha256(f"{settings.EMBED_MODEL}|{settings.EMBED_DIMS}|{text}".encode()).hexdigest()
    return f"qemb:{digest}"


async def get_cached_vector(text: str) -> list[float] | None:
    try:
        async with asyncio.timeout(settings.REDIS_TIMEOUT_S):
            raw = await get_async_cache().get(cache_key(text))
    except Exception as exc:  # noqa: BLE001 - fail open: a cache problem is a miss
        log.warning("query_cache_get_failed", error=str(exc) or exc.__class__.__name__)
        return None
    vec = array("f")
    # Checked on bytes, before unpacking: a value that is not a whole number of float32s (truncated or foreign) would
    # make frombytes raise, and embed_query would then drop to keyword-only retrieval instead of treating it as a miss.
    if not raw or len(raw) != settings.EMBED_DIMS * vec.itemsize:
        return None
    vec.frombytes(raw)
    return vec.tolist()


async def set_cached_vector(text: str, vector: list[float]) -> None:
    try:
        async with asyncio.timeout(settings.REDIS_TIMEOUT_S):
            await get_async_cache().set(cache_key(text), array("f", vector).tobytes(), ex=TTL_S)
    except Exception as exc:  # noqa: BLE001
        log.warning("query_cache_set_failed", error=str(exc) or exc.__class__.__name__)


@dataclass(frozen=True)
class QueryEmbedding:
    vector: list[float] | None      # None = embedding failed or timed out -> keyword-only retrieval
    usage: Usage                    # zero tokens on a cache hit or failure, unless the failed call was billed
    cache_hit: bool = False
    error: str | None = None        # error code for the ai_usage row when the call failed
    ms: int = 0


async def embed_query(provider: LLMProvider, text: str) -> QueryEmbedding:
    """Cache lookup, then the embeddings API, all inside EMBED_QUERY_TIMEOUT_S. Never raises: on failure the caller
    falls back to keyword-only retrieval and adds the 'keyword_only' warning. A failure the provider billed (a vector
    of the wrong size: its ProviderError carries the usage) keeps that usage, so the ledger records what was spent."""
    started = time.perf_counter()
    billed: Usage | None = None

    def elapsed() -> int:
        return int((time.perf_counter() - started) * 1000)

    try:
        async with asyncio.timeout(settings.EMBED_QUERY_TIMEOUT_S):
            cached = await get_cached_vector(text)
            if cached is not None:
                usage = Usage(model=settings.EMBED_MODEL, latency_ms=elapsed())
                return QueryEmbedding(cached, usage, cache_hit=True, ms=elapsed())
            result = await provider.aembed([text])
            await set_cached_vector(text, result.vectors[0])
            return QueryEmbedding(result.vectors[0], result.usage, ms=elapsed())
    except TimeoutError:
        code = "UPSTREAM_TIMEOUT"
    except Exception as exc:  # noqa: BLE001 - ProviderError or anything else: degrade, don't fail the answer
        log.warning("embed_query_failed", error=str(exc))
        code = "UPSTREAM_AI_ERROR"
        billed = getattr(exc, "usage", None)
    usage = billed or Usage(model=settings.EMBED_MODEL, latency_ms=elapsed())
    return QueryEmbedding(None, usage, error=code, ms=elapsed())
