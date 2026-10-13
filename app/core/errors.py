"""One error registry for the whole system.

Every error a client can see has a stable machine-readable `code`, an HTTP status and a human message, wrapped in
one envelope: {"error": {"code", "message", "details", "request_id"}}. Pipeline failures reuse the same codes and are
stored on the document row, so the UI shows the same vocabulary for sync and async failures."""

from enum import StrEnum
from typing import Any

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.request_context import get_request_id

log = structlog.get_logger(__name__)


class ErrorCode(StrEnum):
    # auth / request
    UNAUTHENTICATED = "UNAUTHENTICATED"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    INVALID_CURSOR = "INVALID_CURSOR"
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
        log.exception("unhandled_error", error=str(exc))
        return JSONResponse(envelope(ErrorCode.INTERNAL, "Internal server error."), status_code=500)
