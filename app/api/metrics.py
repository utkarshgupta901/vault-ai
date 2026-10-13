"""Monitoring APIs: documents, processing, AI usage and cost, chat quality, and HTTP performance.

Everything is scoped to the caller's API key except /metrics/system and the queue depth inside /metrics/processing,
which describe the shared system. `window` accepts 15m | 1h | 6h | 24h | 7d | 30d; anything else is a 422
VALIDATION_ERROR (FastAPI validates the Literal, and the error handler wraps it in the standard envelope)."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import CurrentUser, get_current_user
from app.core.db import get_db
from app.schemas.common import ERROR_RESPONSES
from app.schemas.metrics import AIUsageMetrics, ChatMetrics, DocumentStats, ProcessingMetrics, SystemMetrics
from app.services import metrics as svc
from app.services.metrics import GroupBy, Window

router = APIRouter(prefix="/metrics", tags=["metrics"], responses=ERROR_RESPONSES)

User = Annotated[CurrentUser, Depends(get_current_user)]
Db = Annotated[AsyncSession, Depends(get_db)]
WINDOW_HELP = "Look-back window: 15m, 1h, 6h, 24h, 7d or 30d."


@router.get("/documents", response_model=DocumentStats, summary="Document inventory: counts, sizes, tags, time to ready")
async def document_metrics(user: User, db: Db) -> DocumentStats:
    """All-time totals for your documents, breakdowns by status / type / category, top tags, uploads per day
    (last 14 UTC days) and seconds from upload to ready."""
    return await svc.document_stats(db, user.id)


@router.get("/processing", response_model=ProcessingMetrics, summary="Pipeline health: attempts, failures, stage timings")
async def processing_metrics(
    user: User, db: Db, window: Annotated[Window, Query(description=WINDOW_HELP)] = "24h",
) -> ProcessingMetrics:
    """Processing attempts in the window (one per Celery delivery, so retries are visible), success rate, crashed
    workers, p50/p95 per task and per stage, failures by code, hourly throughput, queue depth (global), documents
    in flight and stale ones (no progress for STALE_PROCESSING_MIN minutes; reprocess them)."""
    return await svc.processing_metrics(db, user.id, window)


@router.get("/ai-usage", response_model=AIUsageMetrics, summary="AI calls, tokens and cost (by operation, model or day)")
async def ai_usage_metrics(
    user: User,
    db: Db,
    window: Annotated[Window, Query(description=WINDOW_HELP)] = "7d",
    group_by: Annotated[GroupBy, Query(description="Group rows by operation, model or UTC day.")] = "operation",
) -> AIUsageMetrics:
    """From the ai_usage ledger (one row per model call): totals, cache-hit rate, share of estimated usage
    (interrupted streams), cost per processed document and per chat answer, and today's spend vs your daily limit."""
    return await svc.ai_usage_metrics(db, user.id, window, group_by, user.daily_cost_limit_usd)


@router.get("/chat", response_model=ChatMetrics, summary="Answer quality and latency")
async def chat_metrics(
    user: User, db: Db, window: Annotated[Window, Query(description=WINDOW_HELP)] = "7d",
) -> ChatMetrics:
    """Time to first token and total latency (p50/p95), per-stage timings, answer types, not-found split by path
    (retrieval gate vs model), confidence mix, citations per answer, citation/grounding problems, fallback-model use,
    failed and interrupted answers."""
    return await svc.chat_metrics(db, user.id, window)


@router.get("/system", response_model=SystemMetrics, summary="HTTP performance per route (all users)")
async def system_metrics(
    user: User, db: Db, window: Annotated[Window, Query(description=WINDOW_HELP)] = "1h",
) -> SystemMetrics:
    """Per route template: request count, time-to-first-byte p50/p95 and 4xx / 5xx rates. Global (not per user):
    it describes the service, and route templates contain no tenant data."""
    return await svc.system_metrics(db, window)
