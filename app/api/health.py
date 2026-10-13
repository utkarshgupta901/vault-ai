"""Health checks.

/health/live  — the process is up (used by Docker/Render to restart a dead container). No dependencies.
/health/ready — can we serve traffic? HARD dependencies (DB reachable, pgvector installed, schema at Alembic head)
                return 503 when broken. SOFT dependencies (Redis, workers, object storage, AI key) only mark the
                status "degraded" (still 200): one dead worker pool must not make the load balancer drain every API node.
"""

import time
from typing import Any

import anyio
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.config import settings
from app.core.db import async_session
from app.core.redis import get_async_cache
from app.services import storage
from app.worker.celery_app import celery_app

router = APIRouter(prefix="/health", tags=["health"])
EXPECTED_HEAD = "0001"


@router.get("/live", summary="Liveness probe")
async def live() -> dict[str, str]:
    return {"status": "ok"}


def _worker_queues() -> dict[str, int]:
    replies = celery_app.control.inspect(timeout=1.0).active_queues() or {}
    counts: dict[str, int] = {}
    for queues in replies.values():
        for q in queues:
            counts[q["name"]] = counts.get(q["name"], 0) + 1
    return counts


async def _timed(fn: Any) -> tuple[bool, Any, int]:
    started = time.perf_counter()
    try:
        value = await fn()
        return True, value, int((time.perf_counter() - started) * 1000)
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)[:200], int((time.perf_counter() - started) * 1000)


@router.get("/ready", summary="Readiness probe (hard vs degraded dependencies)")
async def ready() -> JSONResponse:
    checks: dict[str, Any] = {}

    async def db_check() -> dict[str, Any]:
        async with async_session() as s:
            await s.execute(text("SELECT 1"))
            vector = await s.scalar(text("SELECT extversion FROM pg_extension WHERE extname = 'vector'"))
            head = await s.scalar(text("SELECT version_num FROM alembic_version"))
        return {"pgvector": vector, "alembic_head": head}

    ok, value, ms = await _timed(db_check)
    hard_ok = ok and isinstance(value, dict) and bool(value.get("pgvector")) and value.get("alembic_head") == EXPECTED_HEAD
    checks["database"] = {"ok": hard_ok, "ms": ms, **(value if isinstance(value, dict) else {"error": value})}

    async def redis_check() -> bool:
        return bool(await get_async_cache().ping())

    ok, value, ms = await _timed(redis_check)
    checks["redis"] = {"ok": ok, "ms": ms, **({} if ok else {"error": value})}

    async def workers_check() -> dict[str, int]:
        return await anyio.to_thread.run_sync(_worker_queues)

    ok, value, ms = await _timed(workers_check)
    queues = value if ok and isinstance(value, dict) else {}
    checks["workers"] = {"ok": bool(queues.get("ingest")) and bool(queues.get("ai")), "ms": ms, "consumers": queues}

    async def storage_check() -> bool:
        return await anyio.to_thread.run_sync(storage.ping)

    ok, value, ms = await _timed(storage_check)
    checks["storage"] = {"ok": bool(value) if ok else False, "ms": ms}
    checks["ai"] = {"ok": settings.LLM_PROVIDER == "fake" or bool(settings.OPENAI_API_KEY),
                    "provider": settings.LLM_PROVIDER}

    degraded = not all(checks[k]["ok"] for k in ("redis", "workers", "storage", "ai"))
    body = {
        "status": "unavailable" if not hard_ok else ("degraded" if degraded else "ok"),
        "checks": checks,
        "models": {"answer": settings.ANSWER_MODEL, "fast": settings.FAST_MODEL, "insights": settings.INSIGHTS_MODEL,
                   "embeddings": settings.EMBED_MODEL},
    }
    return JSONResponse(body, status_code=200 if hard_ok else 503)
