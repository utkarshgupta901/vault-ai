"""Health checks.

/health/live  — the process is up (used by Docker/Render to restart a dead container). No dependencies.
/health/ready — can we serve traffic? HARD dependencies (DB reachable, pgvector installed, schema at Alembic head)
                return 503 when broken. SOFT dependencies (Redis, workers, object storage, AI) only mark the
                status "degraded" (still 200): one dead worker pool must not make the load balancer drain every API node.

Readiness is public and gets polled from many places at once (load balancer, the dashboard header every 15 s, several
tabs, `make doctor`/smoke/eval preflights), so it must stay cheap and bounded whatever the request rate:
  - The checks run concurrently, each under its own CHECK_TIMEOUT_S. The endpoint always answers within ~2 s, and a
    check that runs out of time reports ok=false, error="timed out" instead of hanging the request.
  - The two checks that need a thread (Celery worker inspect, S3 ping) go through SingleFlightCheck: at most one of
    each runs at a time, concurrent callers share its result, and the result is reused for THREAD_CHECK_TTL_S.
  - The worker inspect runs on its own broker connection, never one borrowed from Celery's shared pool.

Why (incident 2026-10-13): the worker check used to call `inspect().active_queues()` on every request. Celery's
broadcast holds a connection from the shared broker pool (broker_pool_limit=10) while kombu borrows a SECOND pool
connection for its producer. About 10 overlapping calls each held one and waited forever for another (a hold-and-wait
deadlock), and every later publish waited behind them. Each stuck call also pinned one of anyio's 40 worker threads;
once those were gone every `to_thread` call hung (uploads, downloads) and so did new OpenAI connections, because
httpx's truststore SSLContext makes anyio run `wrap_bio` for the TLS handshake in a worker thread.
"""

import threading
import time
from collections.abc import Awaitable, Callable
from typing import Any

import anyio
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.ai import PROVIDER_RESET_AFTER_FAILURES, consecutive_provider_failures
from app.config import settings
from app.core.db import async_session
from app.core.redis import get_async_cache
from app.services import storage
from app.worker.celery_app import celery_app

router = APIRouter(prefix="/health", tags=["health"])
EXPECTED_HEAD = "0001"
CHECK_TIMEOUT_S = 2.0          # per check; checks run concurrently, so this is also ~the endpoint's worst case
THREAD_CHECK_TTL_S = 10.0      # a worker/S3 result is reused this long (the dashboard polls every 15 s)
INSPECT_TIMEOUT_S = 1.0        # how long Celery waits for worker replies (the call always takes about this long)
_WAIT_POLL_S = 0.05


@router.get("/live", summary="Liveness probe")
async def live() -> dict[str, str]:
    return {"status": "ok"}


class SingleFlightCheck:
    """Runs a blocking check in a worker thread, at most one at a time, and reuses its result for `ttl_s`.

    - A result younger than ttl_s is returned at once, without a thread.
    - Otherwise one caller starts a refresh; callers that arrive while it runs wait for that same result instead of
      starting their own (they poll the lock from the event loop, so waiting costs no thread).
    - The threading.Lock is taken and released inside the worker thread. A caller that gives up (CHECK_TIMEOUT_S, the
      thread is abandoned) therefore cannot let a second check start while the first is still running.
    - Failures are cached like successes, so a dead dependency costs one check per TTL, not one per request.
    """

    def __init__(self, fn: Callable[[], Any], ttl_s: float):
        self._fn = fn
        self._ttl_s = ttl_s
        self._lock = threading.Lock()          # held by the thread that is running the check
        self._starting = False                 # a caller in this event loop is handing a refresh to a thread
        self._result: tuple[float, Any, str | None] | None = None   # (finished_at, value, error)

    def reset(self) -> None:
        """Forget the cached result (tests)."""
        self._result = None

    async def get(self) -> Any:
        if not self._fresh():
            if not self._lock.locked() and not self._starting:
                self._starting = True
                try:
                    await anyio.to_thread.run_sync(self._refresh, abandon_on_cancel=True)
                finally:
                    self._starting = False
            # Another caller's refresh is running: share it. Polled, not an Event, because the refresh ends in a thread
            # that may outlive the coroutine that started it (abandoned on timeout), so no task is left to set one.
            while self._lock.locked() or self._starting:  # noqa: ASYNC110
                await anyio.sleep(_WAIT_POLL_S)
        if self._result is None:
            raise RuntimeError("check has not completed yet")
        _, value, error = self._result
        if error is not None:
            raise RuntimeError(error)
        return value

    def _fresh(self) -> bool:
        return self._result is not None and time.monotonic() - self._result[0] < self._ttl_s

    def _refresh(self) -> None:
        """Worker thread. Never raises: the outcome is stored for every caller to read."""
        if not self._lock.acquire(blocking=False):
            return                                         # a refresh is already running; callers wait for it
        try:
            if self._fresh():                              # finished while this one was queued for a thread
                return
            try:
                self._result = (time.monotonic(), self._fn(), None)
            except Exception as exc:  # noqa: BLE001 - reported as the check's error
                self._result = (time.monotonic(), None, str(exc)[:200] or type(exc).__name__)
        finally:
            self._lock.release()


def _worker_queues() -> dict[str, int]:
    """Consumers per queue, from the live workers' replies.

    `connection=` gives the broadcast its own short-lived broker connection, so it never holds a connection from
    Celery's shared pool (the one uploads publish through) while kombu borrows a producer from that pool."""
    with celery_app.connection_for_write() as conn:
        replies = celery_app.control.inspect(timeout=INSPECT_TIMEOUT_S, connection=conn).active_queues() or {}
    counts: dict[str, int] = {}
    for queues in replies.values():
        for q in queues:
            counts[q["name"]] = counts.get(q["name"], 0) + 1
    return counts


workers_check = SingleFlightCheck(_worker_queues, ttl_s=THREAD_CHECK_TTL_S)
storage_check = SingleFlightCheck(storage.ping, ttl_s=THREAD_CHECK_TTL_S)


async def _timed(fn: Callable[[], Awaitable[Any]]) -> tuple[bool, Any, int]:
    started = time.perf_counter()

    def ms() -> int:
        return int((time.perf_counter() - started) * 1000)

    try:
        with anyio.fail_after(CHECK_TIMEOUT_S):
            value = await fn()
        return True, value, ms()
    except TimeoutError:
        return False, f"timed out after {CHECK_TIMEOUT_S:g}s", ms()
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)[:200], ms()


async def _db() -> dict[str, Any]:
    async with async_session() as s:
        await s.execute(text("SELECT 1"))
        vector = await s.scalar(text("SELECT extversion FROM pg_extension WHERE extname = 'vector'"))
        head = await s.scalar(text("SELECT version_num FROM alembic_version"))
    return {"pgvector": vector, "alembic_head": head}


async def _redis() -> bool:
    return bool(await get_async_cache().ping())


def _ai_check() -> dict[str, Any]:
    """Configured, and the last AI calls in this process did not all fail (see app.ai's provider watchdog)."""
    configured = settings.LLM_PROVIDER == "fake" or bool(settings.OPENAI_API_KEY)
    failures = consecutive_provider_failures()
    check: dict[str, Any] = {"ok": configured and failures < PROVIDER_RESET_AFTER_FAILURES,
                             "provider": settings.LLM_PROVIDER, "consecutive_failures": failures}
    if not configured:
        check["error"] = "OPENAI_API_KEY is not set"
    elif not check["ok"]:
        check["error"] = f"last {failures} AI calls failed"
    return check


@router.get("/ready", summary="Readiness probe (hard vs degraded dependencies)")
async def ready() -> JSONResponse:
    probes: dict[str, Callable[[], Awaitable[Any]]] = {
        "database": _db, "redis": _redis, "workers": workers_check.get, "storage": storage_check.get,
    }
    results: dict[str, tuple[bool, Any, int]] = {}

    async def run(name: str) -> None:
        results[name] = await _timed(probes[name])

    async with anyio.create_task_group() as tg:   # concurrently: the slowest check bounds the response, not the sum
        for name in probes:
            tg.start_soon(run, name)

    checks: dict[str, Any] = {}
    ok, value, ms = results["database"]
    hard_ok = ok and isinstance(value, dict) and bool(value.get("pgvector")) and value.get("alembic_head") == EXPECTED_HEAD
    checks["database"] = {"ok": hard_ok, "ms": ms, **(value if isinstance(value, dict) else {"error": value})}

    ok, value, ms = results["redis"]
    checks["redis"] = {"ok": ok, "ms": ms, **({} if ok else {"error": value})}

    ok, value, ms = results["workers"]
    queues = value if ok and isinstance(value, dict) else {}
    checks["workers"] = {"ok": bool(queues.get("ingest")) and bool(queues.get("ai")), "ms": ms, "consumers": queues,
                         **({} if ok else {"error": value})}

    ok, value, ms = results["storage"]
    checks["storage"] = {"ok": ok and bool(value), "ms": ms}
    if not checks["storage"]["ok"]:
        checks["storage"]["error"] = value if not ok else "bucket not reachable"
    checks["ai"] = _ai_check()

    degraded = not all(checks[k]["ok"] for k in ("redis", "workers", "storage", "ai"))
    body = {
        "status": "unavailable" if not hard_ok else ("degraded" if degraded else "ok"),
        "checks": checks,
        "models": {"answer": settings.ANSWER_MODEL, "fast": settings.FAST_MODEL, "insights": settings.INSIGHTS_MODEL,
                   "embeddings": settings.EMBED_MODEL},
    }
    return JSONResponse(body, status_code=200 if hard_ok else 503)
