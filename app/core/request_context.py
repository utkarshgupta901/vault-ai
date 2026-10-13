"""Request ID propagation.

A pure-ASGI middleware (not BaseHTTPMiddleware, which buffers streaming responses) accepts X-Request-ID or generates
one, binds it into structlog's contextvars, and echoes it on the response. The same id is passed into Celery tasks
and stored on processing_runs / ai_usage, so one upload can be traced HTTP -> task -> AI calls."""

import uuid
from contextvars import ContextVar

import structlog
from starlette.types import ASGIApp, Message, Receive, Scope, Send

_request_id: ContextVar[str | None] = ContextVar("request_id", default=None)


def get_request_id() -> str | None:
    return _request_id.get()


def new_request_id() -> str:
    return "req_" + uuid.uuid4().hex[:16]


class RequestIdMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        incoming = dict(scope.get("headers") or []).get(b"x-request-id", b"").decode()[:64]
        request_id = incoming or new_request_id()
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
