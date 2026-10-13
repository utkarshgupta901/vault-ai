"""Request telemetry: one `request_logs` row per API call (method, route template, status, ttfb_ms, total_ms).

Why pure ASGI (never BaseHTTPMiddleware): BaseHTTPMiddleware re-wraps the response body in its own stream, which
buffers SSE/streaming responses and breaks client-disconnect handling. This middleware only *observes* the messages
the app sends:
  - `http.response.start` (status + headers) -> ttfb_ms, time to first byte;
  - the app returning                         -> total_ms, the full duration.
For a streamed chat answer ttfb_ms is the time until headers go out and total_ms is the whole stream, which is why
/metrics/system computes latency percentiles on ttfb_ms.

Why a buffer: inserting a row inside the request would add a DB round trip to every response and turn a DB hiccup
into an API outage. Instead each request appends a dict to an in-process list, and one background task inserts the
whole batch every second (or as soon as 100 rows are waiting) in one short transaction. Telemetry is best effort:
a DB error drops that batch with a warning, and rows still in memory when the process dies (at most ~1 s) are lost.
"""

import asyncio
import re
import time
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import insert
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.db import async_session
from app.core.request_context import get_request_id
from app.models import RequestLog

log = structlog.get_logger(__name__)

LOGGED_PREFIX = "/api/"         # skips /health/*, /docs, /openapi.json, /assets/* and the SPA: not API traffic
FLUSH_INTERVAL_S = 1.0
FLUSH_BATCH_ROWS = 100
MAX_BUFFERED_ROWS = 10_000      # if the DB is down for long, drop new rows instead of growing memory without bound
MAX_ROUTE_LEN = 200
CLIENT_CLOSED_REQUEST = 499     # nginx's convention: the client went away before we sent a response

_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def _normalize_ids(path: str) -> str:
    """Replace path segments that are UUIDs or plain numbers with {id} ('/api/v1' keeps its 'v1')."""
    return "/".join("{id}" if _UUID.match(s) or s.isdigit() else s for s in path.split("/"))


def route_template(scope: Scope) -> str:
    """The request path with ids swapped for names, so every document read groups under one route:
    /api/v1/documents/3f2a.../pages/4 -> /api/v1/documents/{document_id}/pages/{page_no}.

    Routing sets scope["route"] (the matched route) and scope["path_params"]. In FastAPI 0.143 the matched route's
    `.path` does NOT include include_router prefixes (it is "/documents/{document_id}", without "/api/v1"), so we
    rebuild the template from the real path: each segment equal to a path-parameter value becomes "{name}".
    Unmatched requests (404s, uploads rejected by UploadGuard before routing) have no path_params and fall back to
    replacing UUID / number segments with {id}, which keeps the number of distinct routes small either way."""
    path: str = scope.get("path", "")
    params = scope.get("path_params") or {}
    if scope.get("route") is not None and params:
        name_by_value = {str(value): name for name, value in params.items()}
        path = "/".join("{" + name_by_value[s] + "}" if s in name_by_value else s for s in path.split("/"))
    return _normalize_ids(path)[:MAX_ROUTE_LEN]


def _ms_since(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


class RequestLogBuffer:
    """In-process queue of request_logs rows, drained by one lazily started background task.

    The task is started on the first add() rather than at import or app startup: there is no running event loop at
    import time, and main.py's lifespan belongs to WP0. It is restarted if it died or the event loop changed (tests).
    `flush()` holds a lock so a caller (a test, or a shutdown hook) that awaits it knows every row added before the
    call is committed, even if the background task was mid-insert."""

    def __init__(self, *, interval_s: float = FLUSH_INTERVAL_S, batch_rows: int = FLUSH_BATCH_ROWS,
                 max_rows: int = MAX_BUFFERED_ROWS):
        self.interval_s = interval_s
        self.batch_rows = batch_rows
        self.max_rows = max_rows
        self.dropped = 0
        self._rows: list[dict[str, Any]] = []
        self._task: asyncio.Task[None] | None = None
        self._wake: asyncio.Event | None = None
        self._lock: asyncio.Lock | None = None

    def add(self, row: dict[str, Any]) -> None:
        if len(self._rows) >= self.max_rows:
            self.dropped += 1
            return
        self._rows.append(row)
        self._ensure_flusher()
        if len(self._rows) >= self.batch_rows and self._wake is not None:
            self._wake.set()

    def discard_pending(self) -> None:
        """Drop rows not yet written (tests use it to start from a clean slate)."""
        self._rows = []

    async def aclose(self) -> None:
        """Stop the background task and write what is left (call from an app shutdown hook to keep the last second)."""
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        await self.flush()

    def _ensure_flusher(self) -> None:
        loop = asyncio.get_running_loop()
        if self._task is None or self._task.done() or self._task.get_loop() is not loop:
            # Event and Lock bind to the loop they are first used on, so they are recreated with the task.
            self._wake, self._lock = asyncio.Event(), asyncio.Lock()
            self._task = loop.create_task(self._run(), name="request-log-flusher")

    async def _run(self) -> None:
        assert self._wake is not None
        while True:
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.interval_s)
            except TimeoutError:
                pass
            self._wake.clear()
            await self.flush()

    async def flush(self) -> int:
        """Insert everything buffered in one short transaction; returns the number of rows written.
        Never raises: telemetry fails open, so a DB error only costs this batch."""
        if self._lock is None:  # created by the first add(), so nothing was ever buffered
            return 0
        async with self._lock:
            rows, self._rows = self._rows, []
            if not rows:
                return 0
            try:
                async with async_session() as session:
                    await session.execute(insert(RequestLog), rows)
                    await session.commit()
            except Exception as exc:  # noqa: BLE001 - any DB error: drop the batch, keep serving
                log.warning("request_log_flush_failed", rows=len(rows), error=str(exc)[:200])
                return 0
            return len(rows)


request_log_buffer = RequestLogBuffer()


class RequestLogMiddleware:
    """Times every /api request and hands one row to the buffer. Never changes or delays the response."""

    def __init__(self, app: ASGIApp, buffer: RequestLogBuffer | None = None):
        self.app = app
        self.buffer = buffer or request_log_buffer

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope["path"].startswith(LOGGED_PREFIX):
            await self.app(scope, receive, send)
            return

        # get_current_user does `request.state.owner_id = ...`; Starlette stores request.state in scope["state"].
        # Creating the dict here guarantees we read the same dict the route wrote to.
        state: dict[str, Any] = scope.setdefault("state", {})
        started = time.perf_counter()
        ttfb_ms: int | None = None
        status = 500  # if the app raises before responding, the server answers 500

        async def timed_send(message: Message) -> None:
            nonlocal ttfb_ms, status
            if message["type"] == "http.response.start":
                ttfb_ms = _ms_since(started)
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, timed_send)
        except asyncio.CancelledError:
            if ttfb_ms is None:
                status = CLIENT_CLOSED_REQUEST
            raise
        finally:
            self._record(scope, state, status, ttfb_ms, _ms_since(started))

    def _record(self, scope: Scope, state: dict[str, Any], status: int, ttfb_ms: int | None, total_ms: int) -> None:
        try:
            self.buffer.add({
                "request_id": get_request_id(),
                "owner_id": state.get("owner_id"),
                "method": scope["method"],
                "route": route_template(scope),
                "status_code": status,
                "ttfb_ms": ttfb_ms,
                "total_ms": total_ms,
                "created_at": datetime.now(UTC),   # request time, not flush time, so windows are exact
            })
        except Exception as exc:  # noqa: BLE001 - telemetry must never break a response
            log.warning("request_log_record_failed", error=str(exc)[:200])
