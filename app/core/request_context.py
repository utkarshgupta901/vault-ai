"""Request ID propagation.

A pure-ASGI middleware (not BaseHTTPMiddleware, which buffers streaming responses) accepts X-Request-ID or generates
one, binds it into structlog's contextvars, and echoes it on the response. The same id is passed into Celery tasks
and stored on processing_runs / ai_usage, so one upload can be traced HTTP -> task -> AI calls.

An incoming id is only reused if it looks like an id (1-64 of [A-Za-z0-9._:-]); anything else is replaced by a fresh
one. It is echoed in a response header, written to logs and stored in the DB, so arbitrary client text has no business
there, and a non-UTF-8 header used to raise UnicodeDecodeError here, outside every handler (a 500 on every route)."""

import re
import uuid
from contextvars import ContextVar

import structlog
from starlette.types import ASGIApp, Message, Receive, Scope, Send

_request_id: ContextVar[str | None] = ContextVar("request_id", default=None)
_VALID_ID = re.compile(r"[A-Za-z0-9._:-]{1,64}")


def get_request_id() -> str | None:
    return _request_id.get()


def new_request_id() -> str:
    return "req_" + uuid.uuid4().hex[:16]


def incoming_request_id(scope: Scope) -> str | None:
    """The client's X-Request-ID if it is a well-formed id, else None (the caller then generates one)."""
    raw = dict(scope.get("headers") or []).get(b"x-request-id")
    if raw is None:
        return None
    value = raw.decode("utf-8", errors="replace")
    return value if _VALID_ID.fullmatch(value) else None


class RequestIdMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = incoming_request_id(scope) or new_request_id()
        token = _request_id.set(request_id)
        structlog.contextvars.bind_contextvars(request_id=request_id)

        async def send_with_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers") or [])
                headers.append((b"x-request-id", request_id.encode()))
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            structlog.contextvars.unbind_contextvars("request_id")
            _request_id.reset(token)
