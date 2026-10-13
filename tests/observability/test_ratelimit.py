"""Rate limiting and the daily quota, exercised through a tiny FastAPI app that mounts only the guards (so these
tests don't depend on which routers other packages have attached them to)."""

import asyncio
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from decimal import Decimal

import pytest
import redis.asyncio as aioredis
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import ratelimit
from app.core.errors import install_error_handlers
from app.core.ratelimit import (
    CHAT_GUARDS,
    UPLOAD_GUARDS,
    rate_limit,
    seconds_left_in_window,
    seconds_until_utc_midnight,
)
from app.core.redis import get_async_cache
from tests.conftest import DEMO, OTHER
from tests.observability.conftest import add_usage, ago

SetLimit = Callable[[str, int], Awaitable[None]]


def guarded_app() -> FastAPI:
    app = FastAPI()
    install_error_handlers(app)

    @app.get("/limited", dependencies=[Depends(rate_limit("test"))])
    async def limited() -> dict:
        return {"ok": True}

    @app.get("/other-group", dependencies=[Depends(rate_limit("test2"))])
    async def other_group() -> dict:
        return {"ok": True}

    @app.post("/upload", dependencies=UPLOAD_GUARDS)
    async def upload() -> dict:
        return {"ok": True}

    @app.post("/ask", dependencies=CHAT_GUARDS)
    async def ask() -> dict:
        return {"ok": True}

    return app


@pytest.fixture
async def guarded(db: AsyncSession) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=guarded_app()), base_url="http://test") as c:
        yield c


@pytest.fixture
async def set_limit(db: AsyncSession) -> AsyncIterator[SetLimit]:
    """Change a seeded user's per-minute limit for one test; the original value is restored afterwards because the
    users table is shared by the whole session (it is not truncated between tests)."""
    originals = dict((await db.execute(text("SELECT name, rate_limit_per_min FROM users"))).tuples().all())

    async def _set(name: str, limit: int) -> None:
        await db.execute(text("UPDATE users SET rate_limit_per_min = :n WHERE name = :u"), {"n": limit, "u": name})
        await db.commit()

    yield _set
    for name, limit in originals.items():
        await _set(name, limit)


async def away_from_window_edge() -> None:
    """Don't let a test's burst straddle a minute boundary (the counter would reset mid-test)."""
    if time.time() % 60 > 58:
        await asyncio.sleep(60 - time.time() % 60 + 0.05)


# --- rate limit ----------------------------------------------------------------------------------------------------
async def test_third_call_in_a_minute_is_429_with_retry_after(guarded: AsyncClient, set_limit: SetLimit,
                                                              demo_user_id: uuid.UUID) -> None:
    await set_limit("demo", 2)
    await away_from_window_edge()

    assert [(await guarded.get("/limited", headers=DEMO)).status_code for _ in range(2)] == [200, 200]
    r = await guarded.get("/limited", headers=DEMO)

    assert r.status_code == 429
    assert r.json()["error"]["code"] == "RATE_LIMITED"
    assert r.json()["error"]["details"] == {"limit": 2, "window_s": 60}
    assert 1 <= int(r.headers["retry-after"]) <= 60
    # INCR and EXPIRE ran together: the counter exists and expires within its window.
    key = f"rl:test:{demo_user_id}:{int(time.time() // 60)}"
    assert int(await get_async_cache().get(key)) == 3
    assert 0 < await get_async_cache().ttl(key) <= 60


async def test_counters_are_per_user_and_per_group(guarded: AsyncClient, set_limit: SetLimit) -> None:
    await set_limit("demo", 1)
    await away_from_window_edge()
    await guarded.get("/limited", headers=DEMO)
    assert (await guarded.get("/limited", headers=DEMO)).status_code == 429

    assert (await guarded.get("/limited", headers=OTHER)).status_code == 200       # another user
    assert (await guarded.get("/other-group", headers=DEMO)).status_code == 200    # another route group


async def test_upload_group_gets_half_the_limit(guarded: AsyncClient, set_limit: SetLimit) -> None:
    await set_limit("demo", 4)
    await away_from_window_edge()
    statuses = [(await guarded.post("/upload", headers=DEMO)).status_code for _ in range(3)]
    assert statuses == [200, 200, 429]


async def test_redis_down_fails_open(guarded: AsyncClient, set_limit: SetLimit, monkeypatch: pytest.MonkeyPatch) -> None:
    await set_limit("demo", 1)
    unreachable = aioredis.from_url("redis://localhost:1/0", socket_timeout=0.2, socket_connect_timeout=0.2)
    monkeypatch.setattr(ratelimit, "get_async_cache", lambda: unreachable)

    assert [(await guarded.get("/limited", headers=DEMO)).status_code for _ in range(3)] == [200, 200, 200]


class _HangingRedis:
    """A Redis that accepts the connection but never answers (the worst case: no fast 'connection refused')."""

    def pipeline(self, transaction: bool = True) -> "_HangingRedis":
        return self

    def incr(self, key: str) -> "_HangingRedis":
        return self

    def expire(self, key: str, seconds: int) -> "_HangingRedis":
        return self

    async def execute(self) -> list:
        await asyncio.sleep(10)
        return [1, True]


async def test_slow_redis_is_cut_off_at_the_deadline(guarded: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ratelimit, "get_async_cache", lambda: _HangingRedis())
    started = time.perf_counter()
    r = await guarded.get("/limited", headers=DEMO)
    assert r.status_code == 200
    assert time.perf_counter() - started < 1.0         # 200 ms deadline, not 10 s


async def test_unauthenticated_request_is_401_before_counting(guarded: AsyncClient) -> None:
    r = await guarded.get("/limited")
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "UNAUTHENTICATED"


def test_retry_after_counts_down_to_the_end_of_the_window() -> None:
    assert seconds_left_in_window(120.0) == 60        # first instant of a window
    assert seconds_left_in_window(150.2) == 30        # ceil(29.8)
    assert seconds_left_in_window(179.9) == 1         # never 0


def test_rate_limit_returns_one_dependency_per_group() -> None:
    # Same object every time, so FastAPI's per-request cache runs it once even if attached twice.
    assert rate_limit("chat") is rate_limit("chat")
    assert rate_limit("chat") is not rate_limit("upload")


# --- daily quota ---------------------------------------------------------------------------------------------------
async def test_quota_reached_returns_429_until_utc_midnight(guarded: AsyncClient, db: AsyncSession,
                                                            demo_user_id: uuid.UUID) -> None:
    await add_usage(db, demo_user_id, "answer", cost_usd=Decimal("19.99"))
    await db.commit()
    assert (await guarded.post("/ask", headers=DEMO)).status_code == 200      # $19.99 < $20.00

    await add_usage(db, demo_user_id, "answer", cost_usd=Decimal("0.01"))
    await db.commit()
    r = await guarded.post("/ask", headers=DEMO)

    assert r.status_code == 429
    body = r.json()["error"]
    assert body["code"] == "QUOTA_EXCEEDED"
    assert body["details"] == {"spent_usd": 20.0, "limit_usd": 20.0}
    expected = seconds_until_utc_midnight(datetime.now(UTC))
    assert abs(int(r.headers["retry-after"]) - expected) <= 2
    assert (await guarded.post("/ask", headers=OTHER)).status_code == 200     # other users have their own budget


async def test_yesterdays_spend_does_not_count(guarded: AsyncClient, db: AsyncSession,
                                               demo_user_id: uuid.UUID) -> None:
    await add_usage(db, demo_user_id, "answer", cost_usd=Decimal("500"), created_at=ago(days=1, hours=1))
    await db.commit()
    assert (await guarded.post("/ask", headers=DEMO)).status_code == 200


def test_seconds_until_utc_midnight() -> None:
    assert seconds_until_utc_midnight(datetime(2026, 10, 13, 23, 59, 30, tzinfo=UTC)) == 30
    assert seconds_until_utc_midnight(datetime(2026, 10, 13, 0, 0, 0, tzinfo=UTC)) == 86_400
