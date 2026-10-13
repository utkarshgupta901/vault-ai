"""API-key authentication with owner scoping.

Clients send `X-API-Key`. We store only sha256(key), look the user up, and every service function then takes
`owner_id` so a user can only ever see their own documents, sessions and metrics. Another owner's resource returns
404 (not 403) so ids can't be probed.

The dependency opens its own short DB session (instead of reusing a request-scoped one) so it also works on
streaming routes, where a request-scoped session would stay open for the whole stream."""

import hashlib
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


@dataclass(frozen=True)
class CurrentUser:
    id: uuid.UUID
    name: str
    daily_cost_limit_usd: Decimal
    rate_limit_per_min: int


def hash_api_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


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
