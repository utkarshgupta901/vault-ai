"""RequestLogMiddleware: rows land in request_logs with the route template, owner, request id and timings; the
middleware never buffers streams and never lets a telemetry failure reach the client."""

import asyncio
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import middleware
from app.core.errors import install_error_handlers
from app.core.middleware import (
    UNMATCHED_ROUTE,
    RequestLogBuffer,
    RequestLogMiddleware,
    request_log_buffer,
    route_template,
)
from tests.conftest import DEMO, upload


class CaptureBuffer:
    """Stands in for the DB-backed buffer: the middleware only ever calls add()."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def add(self, row: dict[str, Any]) -> None:
        self.rows.append(row)


async def logged_rows(db: AsyncSession) -> list[dict[str, Any]]:
    await request_log_buffer.flush()
    result = await db.execute(text("SELECT * FROM request_logs ORDER BY id"))
    return [dict(r) for r in result.mappings()]


# --- through the real app (main.py adds the middleware automatically) ----------------------------------------------
async def test_api_calls_are_logged_with_route_templates(client: AsyncClient, db: AsyncSession,
                                                         demo_user_id: uuid.UUID, quiet_request_logs: None) -> None:
    created = await upload(client, "a.txt", b"Larkspur handbook: 18 PTO days per year.")
    doc_id = created.json()["document"]["id"]
    got = await client.get(f"/api/v1/documents/{doc_id}", headers=DEMO)
    await client.get(f"/api/v1/documents/{doc_id}/pages/1", headers=DEMO)
    await client.get("/api/v1/documents")                     # no key -> 401, no owner
    await client.get("/health/live")                          # not API traffic -> not logged

    rows = await logged_rows(db)

    assert [(r["method"], r["route"], r["status_code"]) for r in rows] == [
        ("POST", "/api/v1/documents", 202),
        ("GET", "/api/v1/documents/{document_id}", 200),
        ("GET", "/api/v1/documents/{document_id}/pages/{page_no}", 200),
        ("GET", "/api/v1/documents", 401),
    ]
    assert [r["owner_id"] for r in rows] == [demo_user_id, demo_user_id, demo_user_id, None]
    assert rows[1]["request_id"] == got.headers["x-request-id"]
    assert all(0 <= r["ttfb_ms"] <= r["total_ms"] for r in rows)


async def test_unmatched_paths_share_one_bucket(client: AsyncClient, db: AsyncSession,
                                                quiet_request_logs: None) -> None:
    """Unmatched paths are client-controlled text and /metrics/system is global, so they are never stored raw."""
    assert (await client.get(f"/api/v1/nope/{uuid.uuid4()}/42", headers=DEMO)).status_code == 404
    assert (await client.get("/api/v1/hello-other-tenants-visit-evil.example")).status_code == 404
    rejected = await client.post("/api/v1/documents", files={"file": ("a.txt", b"x")})   # UploadGuard: no key
    assert rejected.status_code == 401
    rows = await logged_rows(db)
    assert [(row["route"], row["status_code"]) for row in rows] == [
        (UNMATCHED_ROUTE, 404), (UNMATCHED_ROUTE, 404), (UNMATCHED_ROUTE, 401)]


@dataclass
class StubRoute:
    include_in_schema: bool = True


def test_route_template_from_path_params() -> None:
    doc_id = str(uuid.uuid4())
    matched = {"path": f"/api/v1/documents/{doc_id}/pages/4", "route": StubRoute(),
               "path_params": {"document_id": doc_id, "page_no": "4"}}
    assert route_template(matched) == "/api/v1/documents/{document_id}/pages/{page_no}"
    assert route_template({"path": "/api/v1/documents", "route": StubRoute()}) == "/api/v1/documents"  # 'v1' kept
    assert route_template({"path": "/api/v1/documents"}) == UNMATCHED_ROUTE                 # no route matched
    spa_catch_all = {"path": "/api/v1/hello-evil", "route": StubRoute(include_in_schema=False),
                     "path_params": {"path": "api/v1/hello-evil"}}
    assert route_template(spa_catch_all) == UNMATCHED_ROUTE


# --- on a small app, with a capture buffer -------------------------------------------------------------------------
def small_app(buffer: CaptureBuffer) -> FastAPI:
    app = FastAPI()
    install_error_handlers(app)
    app.add_middleware(RequestLogMiddleware, buffer=buffer)

    @app.get("/api/v1/stream")
    async def stream() -> StreamingResponse:
        async def chunks() -> AsyncIterator[bytes]:
            for i in range(3):
                await asyncio.sleep(0.05)
                yield f"chunk{i};".encode()
        return StreamingResponse(chunks(), media_type="text/plain")

    @app.get("/api/v1/boom")
    async def boom() -> None:
        raise RuntimeError("bug")

    @app.get("/docs-like")
    async def not_api() -> dict:
        return {}

    return app


async def test_streaming_response_passes_through_and_ttfb_is_before_total() -> None:
    buffer = CaptureBuffer()
    async with AsyncClient(transport=ASGITransport(app=small_app(buffer)), base_url="http://t") as c:
        r = await c.get("/api/v1/stream")
    assert r.text == "chunk0;chunk1;chunk2;"
    (row,) = buffer.rows
    assert (row["route"], row["status_code"]) == ("/api/v1/stream", 200)
    assert row["total_ms"] >= 150 and row["ttfb_ms"] < row["total_ms"] - 50   # headers went out before the body


async def test_unhandled_exception_is_logged_as_500() -> None:
    buffer = CaptureBuffer()
    transport = ASGITransport(app=small_app(buffer), raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        assert (await c.get("/api/v1/boom")).status_code == 500
        await c.get("/docs-like")                      # outside /api: not logged
    assert [(r["route"], r["status_code"]) for r in buffer.rows] == [("/api/v1/boom", 500)]


async def test_raw_asgi_messages_are_forwarded_unchanged() -> None:
    """A pure-ASGI middleware must hand every message to the server as-is (no buffering, no re-wrapping)."""
    messages = [
        {"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/event-stream")]},
        {"type": "http.response.body", "body": b"event: token\n\n", "more_body": True},
        {"type": "http.response.body", "body": b"event: done\n\n", "more_body": False},
    ]

    async def app(scope: Any, receive: Any, send: Any) -> None:
        for m in messages:
            await send(m)

    sent: list[dict] = []

    async def send(message: dict) -> None:
        sent.append(message)

    buffer = CaptureBuffer()
    scope = {"type": "http", "method": "POST", "path": "/api/v1/chat/sessions/x/messages/stream", "headers": []}
    await RequestLogMiddleware(app, buffer=buffer)(scope, None, send)
    assert sent == messages
    assert buffer.rows[0]["status_code"] == 200


# --- the buffer ----------------------------------------------------------------------------------------------------
def _row(**overrides: Any) -> dict[str, Any]:
    return {"request_id": "req_test", "owner_id": None, "method": "GET", "route": "/api/v1/x", "status_code": 200,
            "ttfb_ms": 1, "total_ms": 2} | overrides


async def test_buffer_flushes_in_the_background(db: AsyncSession, quiet_request_logs: None) -> None:
    buffer = RequestLogBuffer(interval_s=0.05)
    buffer.add(_row(route="/api/v1/background"))
    await asyncio.sleep(0.3)                           # no explicit flush: the background task writes it
    count = await db.scalar(text("SELECT count(*) FROM request_logs WHERE route = '/api/v1/background'"))
    await buffer.aclose()
    assert count == 1


async def test_db_failure_drops_the_batch_without_raising(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken_session() -> Any:
        raise ConnectionError("database is down")

    monkeypatch.setattr(middleware, "async_session", broken_session)
    buffer = RequestLogBuffer(interval_s=60)
    buffer.add(_row())
    assert await buffer.flush() == 0                   # logged and dropped, not raised
    assert await buffer.flush() == 0                   # and not retried forever
    await buffer.aclose()


async def test_buffer_is_bounded_when_the_db_is_unreachable() -> None:
    buffer = RequestLogBuffer(interval_s=60, max_rows=2)
    for _ in range(3):
        buffer.add(_row())
    assert buffer.dropped == 1
    buffer.discard_pending()
    await buffer.aclose()


async def test_app_shutdown_writes_the_rows_still_buffered(db: AsyncSession, quiet_request_logs: None,
                                                           monkeypatch: pytest.MonkeyPatch) -> None:
    """The requests of the last second before a restart or deploy are still logged: the lifespan closes the buffer
    the middleware writes to. Its flusher is restarted with a long interval first, so only that close can write."""
    from app import main
    from app.rag import tokens

    await request_log_buffer.aclose()
    monkeypatch.setattr(request_log_buffer, "interval_s", 3600.0)
    monkeypatch.setattr(tokens, "warm_up", lambda: None)
    async with main.lifespan(FastAPI()):
        request_log_buffer.add(_row(route="/api/v1/last-request"))
    count = await db.scalar(text("SELECT count(*) FROM request_logs WHERE route = '/api/v1/last-request'"))
    assert count == 1
