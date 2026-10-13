"""Per-user rate limits and the soft daily AI-cost quota, as FastAPI dependencies.

Rate limit: a fixed one-minute window per (route group, user) in Redis (Valkey db1):
    key = rl:{group}:{user_id}:{epoch_minute}
    MULTI; INCR key; EXPIRE key 60; EXEC
INCR is atomic, so two concurrent requests can never both read "59" and both pass. INCR and EXPIRE go in one MULTI
transaction so a crash between them cannot leave a counter without a TTL: a counter never outlives its window.
Trade-off (said out loud in the demo): a fixed window allows a burst of up to 2x the limit across a minute boundary;
GCRA / a sliding window would fix that and is deferred.

Redis is an optional dependency: if it is down or slower than REDIS_TIMEOUT_S (200 ms) we log a warning and let the
request through (fail open). Money is still protected by the daily quota, which is read from Postgres.

Quota: sum(ai_usage.cost_usd) since UTC midnight >= users.daily_cost_limit_usd -> 429 QUOTA_EXCEEDED until midnight.
It is SOFT (check-then-act): N requests arriving together can all pass the check before any of them records its cost,
so the overshoot is bounded by in-flight requests x the cost of one answer (about $0.03). A hard quota would reserve
an estimated cost before the call and settle it after; deferred.

Usage (route groups have separate counters, so a burst of uploads never blocks chatting):
    router = APIRouter(dependencies=CHAT_GUARDS)            # or @router.post(..., dependencies=AI_GUARDS)
"""

import asyncio
import math
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from functools import cache
from typing import Annotated

import structlog
from fastapi import Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.auth import CurrentUser, get_current_user
from app.core.db import async_session
from app.core.errors import AppError, ErrorCode
from app.core.redis import get_async_cache
from app.models import AIUsage

log = structlog.get_logger(__name__)

WINDOW_S = 60
HALF_LIMIT_GROUPS = {"upload"}   # each upload costs S3 + extraction + embeddings, so it gets half the request budget


def limit_for(group: str, user: CurrentUser) -> int:
    limit = user.rate_limit_per_min
    return max(1, limit // 2) if group in HALF_LIMIT_GROUPS else limit


def seconds_left_in_window(now: float) -> int:
    """Seconds until the current fixed window ends (what Retry-After tells the client); always >= 1."""
    return max(1, math.ceil(WINDOW_S - now % WINDOW_S))


async def _increment(key: str) -> int | None:
    """INCR + EXPIRE in one MULTI/EXEC. Returns None when Redis is unavailable (the caller fails open).

    The whole call shares one 200 ms deadline: the client already has 200 ms socket timeouts, but redis-py retries
    failed commands with backoff by default, so only an outer deadline really bounds the added latency."""
    try:
        async with asyncio.timeout(settings.REDIS_TIMEOUT_S):
            count, _ = await get_async_cache().pipeline(transaction=True).incr(key).expire(key, WINDOW_S).execute()
        return int(count)
    except Exception as exc:  # noqa: BLE001 - any Redis/network error or timeout: fail open
        log.warning("rate_limit_unavailable", key=key, error=str(exc)[:200] or type(exc).__name__)
        return None


@cache
def rate_limit(group: str) -> Callable[..., Awaitable[None]]:
    """Dependency factory: `Depends(rate_limit("chat"))`.

    Cached so rate_limit("chat") is the same function object everywhere: FastAPI's per-request dependency cache then
    runs it once even if it is attached at both router and route level (no double counting). It depends on
    get_current_user, which FastAPI also caches per request, so the API key is checked only once."""

    async def check_rate_limit(user: Annotated[CurrentUser, Depends(get_current_user)]) -> None:
        limit = limit_for(group, user)
        now = time.time()
        count = await _increment(f"rl:{group}:{user.id}:{int(now // WINDOW_S)}")
        if count is not None and count > limit:
            raise AppError(
                ErrorCode.RATE_LIMITED, 429,
                f"Too many {group} requests: the limit is {limit} per minute. Try again shortly.",
                {"limit": limit, "window_s": WINDOW_S},
                headers={"Retry-After": str(seconds_left_in_window(now))},
            )

    check_rate_limit.__name__ = f"rate_limit_{group}"
    return check_rate_limit


def utc_midnight(now: datetime) -> datetime:
    return now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)


def seconds_until_utc_midnight(now: datetime) -> int:
    return max(1, math.ceil((utc_midnight(now) + timedelta(days=1) - now).total_seconds()))


async def today_spend_usd(db: AsyncSession, owner_id: uuid.UUID, now: datetime | None = None) -> Decimal:
    """AI spend since UTC midnight. Served by the ix_ai_usage_owner_created index (owner_id, created_at)."""
    since = utc_midnight(now or datetime.now(UTC))
    spent = await db.scalar(
        select(func.coalesce(func.sum(AIUsage.cost_usd), 0))
        .where(AIUsage.owner_id == owner_id, AIUsage.created_at >= since)
    )
    return Decimal(spent)


async def enforce_daily_quota(user: Annotated[CurrentUser, Depends(get_current_user)]) -> None:
    """Soft daily cost quota (see module docstring). Opens its own short session, like get_current_user, so it also
    works in front of streaming routes where a request-scoped session would stay open for the whole stream."""
    now = datetime.now(UTC)
    async with async_session() as db:
        spent = await today_spend_usd(db, user.id, now)
    limit = Decimal(user.daily_cost_limit_usd)
    if spent >= limit:
        raise AppError(
            ErrorCode.QUOTA_EXCEEDED, 429,
            f"Daily AI budget of ${limit:.2f} reached; it resets at 00:00 UTC.",
            {"spent_usd": float(spent), "limit_usd": float(limit)},
            headers={"Retry-After": str(seconds_until_utc_midnight(now))},
        )


# Ready-made guard lists for the routers (attach with `dependencies=...`):
CHAT_GUARDS = [Depends(rate_limit("chat")), Depends(enforce_daily_quota)]   # ask / stream (LLM calls)
AI_GUARDS = [Depends(rate_limit("ai")), Depends(enforce_daily_quota)]       # summaries, compare (LLM calls)
UPLOAD_GUARDS = [Depends(rate_limit("upload"))]                             # uploads: half the per-minute limit
