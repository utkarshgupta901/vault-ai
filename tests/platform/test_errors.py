"""Unhandled errors keep their request id (envelope, X-Request-ID header and log line), and the incoming X-Request-ID
is only reused when it looks like an id."""

from collections.abc import AsyncIterator

import pytest
import structlog
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from httpx import ASGITransport, AsyncClient
from structlog.testing import capture_logs

from app.core.request_context import incoming_request_id
from app.main import create_app


@pytest.fixture
def app_with_bugs() -> FastAPI:
    """The real middleware stack (create_app) plus two routes that fail."""
    app = create_app()

    @app.get("/api/v1/__boom")
    async def boom() -> None:
        raise RuntimeError("fromisoformat: argument must be str")

    @app.get("/api/v1/__boom-mid-stream")
    async def boom_mid_stream() -> StreamingResponse:
        async def chunks() -> AsyncIterator[bytes]:
            yield b"partial"
            raise RuntimeError("stream broke")
        return StreamingResponse(chunks())

    return app


async def test_unhandled_error_keeps_the_request_id(app_with_bugs: FastAPI) -> None:
    async with AsyncClient(transport=ASGITransport(app=app_with_bugs), base_url="http://t") as c:
        with capture_logs(processors=[structlog.contextvars.merge_contextvars]) as logs:
            r = await c.get("/api/v1/__boom", headers={"X-Request-ID": "trace-500"})
    assert r.status_code == 500
    assert r.headers["x-request-id"] == "trace-500"
    assert r.json() == {"error": {"code": "INTERNAL", "message": "Internal server error.", "details": {},
                                  "request_id": "trace-500"}}
    errors = [e for e in logs if e["event"] == "unhandled_error"]
    assert len(errors) == 1                              # logged once, not again by the outer fallback handler
    assert errors[0]["request_id"] == "trace-500"
    assert errors[0]["path"] == "/api/v1/__boom"


async def test_generated_request_id_is_used_when_none_is_sent(app_with_bugs: FastAPI) -> None:
    async with AsyncClient(transport=ASGITransport(app=app_with_bugs), base_url="http://t") as c:
        r = await c.get("/api/v1/__boom")
    assert r.headers["x-request-id"].startswith("req_")
    assert r.json()["error"]["request_id"] == r.headers["x-request-id"]


async def test_error_after_the_response_started_is_reraised(app_with_bugs: FastAPI) -> None:
    """Too late for an envelope: the error goes up to the server, which aborts the connection (no second response)."""
    async with AsyncClient(transport=ASGITransport(app=app_with_bugs), base_url="http://t") as c:
        with pytest.raises(RuntimeError, match="stream broke"):
            await c.get("/api/v1/__boom-mid-stream")


@pytest.mark.parametrize(("raw", "expected"), [
    (b"rvprobe-404", "rvprobe-404"),
    (b"web:1a2b.C_3-x", "web:1a2b.C_3-x"),
    (b"a" * 64, "a" * 64),
    (b"a" * 65, None),                                  # too long: replaced, not truncated
    (b"abc\xff\xfe", None),                             # not UTF-8 (used to raise -> 500 on every route)
    (b"<script>alert(1)</script>", None),
    (b"with space", None),
    (b"", None),
])
def test_incoming_request_id_is_validated(raw: bytes, expected: str | None) -> None:
    assert incoming_request_id({"headers": [(b"x-request-id", raw)]}) == expected


async def test_non_utf8_request_id_gets_a_fresh_one(app_with_bugs: FastAPI) -> None:
    async with AsyncClient(transport=ASGITransport(app=app_with_bugs), base_url="http://t") as c:
        r = await c.get("/health/live", headers={"X-Request-ID": b"abc\xff\xfe"})
    assert r.status_code == 200
    assert r.headers["x-request-id"].startswith("req_")
