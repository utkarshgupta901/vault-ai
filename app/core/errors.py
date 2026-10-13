"""One error registry for the whole system.

Every error a client can see has a stable machine-readable `code`, an HTTP status and a human message, wrapped in
one envelope: {"error": {"code", "message", "details", "request_id"}}. Pipeline failures reuse the same codes and are
stored on the document row, so the UI shows the same vocabulary for sync and async failures."""

import json
from enum import StrEnum
from typing import Any

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.request_context import get_request_id

log = structlog.get_logger(__name__)


class ErrorCode(StrEnum):
    # auth / request
    UNAUTHENTICATED = "UNAUTHENTICATED"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    INVALID_CURSOR = "INVALID_CURSOR"
    PAYLOAD_TOO_LARGE = "PAYLOAD_TOO_LARGE"   # a non-upload request body over the generic cap (upload_guard.py)
    NOT_FOUND = "NOT_FOUND"
    RATE_LIMITED = "RATE_LIMITED"
    QUOTA_EXCEEDED = "QUOTA_EXCEEDED"
    INTERNAL = "INTERNAL"
    # uploads
    LENGTH_REQUIRED = "LENGTH_REQUIRED"
    FILE_TOO_LARGE = "FILE_TOO_LARGE"
    UNSUPPORTED_MEDIA_TYPE = "UNSUPPORTED_MEDIA_TYPE"
    EMPTY_FILE = "EMPTY_FILE"
    UNSAFE_ARCHIVE = "UNSAFE_ARCHIVE"
    QUEUE_BACKPRESSURE = "QUEUE_BACKPRESSURE"
    QUEUE_UNAVAILABLE = "QUEUE_UNAVAILABLE"
    # documents
    DOCUMENT_NOT_FOUND = "DOCUMENT_NOT_FOUND"
    DOCUMENT_NOT_READY = "DOCUMENT_NOT_READY"
    ALREADY_PROCESSING = "ALREADY_PROCESSING"
    PAGE_NOT_FOUND = "PAGE_NOT_FOUND"
    COMPARE_TOO_LARGE = "COMPARE_TOO_LARGE"
    # chat
    SESSION_NOT_FOUND = "SESSION_NOT_FOUND"
    NO_READY_DOCUMENTS = "NO_READY_DOCUMENTS"
    SESSION_BUSY = "SESSION_BUSY"
    MESSAGE_IN_PROGRESS = "MESSAGE_IN_PROGRESS"
    UPSTREAM_AI_ERROR = "UPSTREAM_AI_ERROR"
    UPSTREAM_TIMEOUT = "UPSTREAM_TIMEOUT"
    # pipeline (stored on documents.error_code / processing_runs.error_code)
    ENCRYPTED_PDF = "ENCRYPTED_PDF"
    CORRUPT_FILE = "CORRUPT_FILE"
    NO_EXTRACTABLE_TEXT = "NO_EXTRACTABLE_TEXT"
    TOO_MANY_PAGES = "TOO_MANY_PAGES"
    STORAGE_ERROR = "STORAGE_ERROR"
    PROCESSING_TIMEOUT = "PROCESSING_TIMEOUT"
    PROCESSING_CRASHED = "PROCESSING_CRASHED"
    WORKER_LOST = "WORKER_LOST"
    SUPERSEDED = "SUPERSEDED"                 # processing_runs only: a reprocess took over this attempt
    PROCESSING_ERROR = "PROCESSING_ERROR"     # unexpected exception in a worker (retryable via reprocess)
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    PROVIDER_BAD_REQUEST = "PROVIDER_BAD_REQUEST"
    INSIGHTS_REFUSED = "INSIGHTS_REFUSED"
    INSIGHTS_TRUNCATED = "INSIGHTS_TRUNCATED"
    INSIGHTS_PARSE_FAILED = "INSIGHTS_PARSE_FAILED"


class AppError(Exception):
    """Raise anywhere in the API; the handler turns it into the envelope."""

    def __init__(
        self,
        code: ErrorCode,
        status: int,
        message: str,
        details: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.status = status
        self.message = message
        self.details = details or {}
        self.headers = headers


class PermanentError(Exception):
    """Pipeline failure that retrying cannot fix (corrupt file, no text, ...). Fails the run immediately."""

    def __init__(self, code: ErrorCode, message: str | None = None):
        super().__init__(message or code.value)
        self.code = code
        self.message = message or code.value


def not_found(code: ErrorCode = ErrorCode.NOT_FOUND, what: str = "Resource") -> AppError:
    # Another owner's resource also returns 404 (not 403): we don't leak that the id exists.
    return AppError(code, 404, f"{what} not found.")


def envelope(code: str, message: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, "details": details or {}, "request_id": get_request_id()}}


async def send_error(send: Send, status: int, code: str, message: str, *, close_connection: bool = False) -> None:
    """Send the envelope from pure-ASGI middleware, which runs outside FastAPI's exception handlers.
    close_connection: for rejections made before the body was read, so the server need not drain it for keep-alive."""
    body = json.dumps(envelope(code, message)).encode()
    headers = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]
    if close_connection:
        headers.append((b"connection", b"close"))
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


class UnhandledErrorMiddleware:
    """Turns an unhandled exception into the INTERNAL envelope while the request id is still bound.

    Why not only the `Exception` handler below: Starlette runs that handler in ServerErrorMiddleware, the outermost
    layer, after RequestIdMiddleware has already unbound the id. Every 500 then had request_id null, no X-Request-ID
    header and an `unhandled_error` log line without the id, i.e. exactly the errors you most need to trace.
    main.py installs this middleware directly inside RequestIdMiddleware, so the log line carries request_id and the
    response goes out through RequestIdMiddleware's send (which adds X-Request-ID). The exception is handled here, so
    the outer handler never sends a second response.

    If the response has already started (a stream failed half-way) an envelope can't be sent any more: we log and
    re-raise, and the server aborts the connection."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        response_started = False

        async def tracking_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, receive, tracking_send)
        except Exception as exc:
            log.exception("unhandled_error", error=str(exc), method=scope.get("method"), path=scope.get("path"))
            if response_started:
                raise
            await send_error(send, 500, ErrorCode.INTERNAL, "Internal server error.")


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(envelope(exc.code, exc.message, exc.details), status_code=exc.status, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [
            {"loc": list(e.get("loc", [])), "msg": e.get("msg"), "type": e.get("type")} for e in exc.errors()
        ]
        return JSONResponse(
            envelope(ErrorCode.VALIDATION_ERROR, "Request validation failed.", {"errors": errors}), status_code=422
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = ErrorCode.NOT_FOUND if exc.status_code == 404 else f"HTTP_{exc.status_code}"
        return JSONResponse(envelope(code, str(exc.detail)), status_code=exc.status_code, headers=exc.headers)

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
        # Fallback only: UnhandledErrorMiddleware handles errors first, with the request id still bound.
        log.exception("unhandled_error", error=str(exc))
        return JSONResponse(envelope(ErrorCode.INTERNAL, "Internal server error."), status_code=500)
