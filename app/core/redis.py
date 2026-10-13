"""Redis/Valkey clients for cache + rate limits (db1). The broker (db0) is owned by Celery.

Cache and rate limiting are *optional* dependencies: every call has a 200 ms timeout and callers fail open,
so a Redis hiccup degrades performance, never correctness."""

from functools import cache

import redis
import redis.asyncio as aioredis

from app.config import settings


@cache
def get_async_cache() -> aioredis.Redis:
    return aioredis.from_url(
        settings.CACHE_URL,
        socket_timeout=settings.REDIS_TIMEOUT_S,
        socket_connect_timeout=settings.REDIS_TIMEOUT_S,
        decode_responses=False,
    )


@cache
def get_sync_cache() -> redis.Redis:
    return redis.from_url(settings.CACHE_URL, socket_timeout=1.0, socket_connect_timeout=1.0)


@cache
def get_sync_broker() -> redis.Redis:
    """Only used to read queue depth (LLEN) for backpressure and metrics."""
    return redis.from_url(settings.BROKER_URL, socket_timeout=1.0, socket_connect_timeout=1.0)
