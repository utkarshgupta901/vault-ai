"""API-key authentication with owner scoping.

Clients send `X-API-Key`. We store only sha256(key), look the user up, and every service function then takes
`owner_id` so a user can only ever see their own documents, sessions and metrics. Another owner's resource returns
404 (not 403) so ids can't be probed.

The dependency opens its own short DB session (instead of reusing a request-scoped one) so it also works on
streaming routes, where a request-scoped session would stay open for the whole stream."""

import hashlib
import time
import uuid
from dataclasses import dataclass
from decimal import Decimal

import structlog
from fastapi import Request, Security
from fastapi.security import APIKeyHeader
from sqlalchemy import select

from app.core.db import async_session
from app.core.errors import AppError, ErrorCode
from app.models import User

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False, description="Your Vault API key.")

KNOWN_KEY_TTL_S = 60.0
_MAX_KNOWN_KEYS = 1024
_known_key_hashes: dict[str, float] = {}   # sha256(valid key) -> monotonic expiry


@dataclass(frozen=True)
class CurrentUser:
    id: uuid.UUID
    name: str
    daily_cost_limit_usd: Decimal
    rate_limit_per_min: int


def hash_api_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


async def is_known_api_key(api_key: str) -> bool:
    """Does any user have this key? A cheap pre-check for UploadGuard, which must reject a bogus key BEFORE the
    request body is read (FastAPI reads the body before dependencies such as get_current_user run).

    Only hashes of valid keys are cached (for KNOWN_KEY_TTL_S), so a stream of random keys can't grow the cache; each
    unknown key costs one lookup on the unique index users.api_key_hash. It is a filter, not the authority:
    get_current_user still checks the database on every request, so a key revoked within the TTL is still refused
    (only its body gets read first)."""
    digest = hash_api_key(api_key)
    now = time.monotonic()
    if _known_key_hashes.get(digest, 0.0) > now:
        return True
    async with async_session() as session:
        found = await session.scalar(select(User.id).where(User.api_key_hash == digest))
    if found is None:
        return False
    if len(_known_key_hashes) >= _MAX_KNOWN_KEYS:   # only real keys land here; the bound is a safety net
        _known_key_hashes.clear()
    _known_key_hashes[digest] = now + KNOWN_KEY_TTL_S
    return True


async def get_current_user(request: Request, api_key: str | None = Security(api_key_header)) -> CurrentUser:
    if not api_key:
        raise AppError(ErrorCode.UNAUTHENTICATED, 401, "Missing X-API-Key header.")
    async with async_session() as session:
        user = await session.scalar(select(User).where(User.api_key_hash == hash_api_key(api_key)))
    if user is None:
        raise AppError(ErrorCode.UNAUTHENTICATED, 401, "Invalid API key.")
    request.state.owner_id = user.id
    structlog.contextvars.bind_contextvars(owner=user.name)
    return CurrentUser(user.id, user.name, user.daily_cost_limit_usd, user.rate_limit_per_min)
