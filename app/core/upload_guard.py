"""Reject bad requests BEFORE the body is read.

FastAPI reads the request body (multipart files are spooled to disk, JSON is buffered in memory) before route
dependencies run, so auth/size checks inside a route would come after a client already made us read the whole body.
This pure-ASGI guard runs first. Cheap header checks come before the one that needs the database.

Uploads (POST /api/v1/documents):
  - no X-API-Key header        -> 401
  - no Content-Length          -> 411 (chunked uploads can't be size-checked up front)
  - Content-Length too large   -> 413 FILE_TOO_LARGE
  - unknown X-API-Key          -> 401 (auth.is_known_api_key: one indexed lookup, valid keys cached in-process), so
                                  a made-up key can't make us spool 25 MB to disk before get_current_user says no
Every other /api POST/PUT/PATCH (small JSON bodies):
  - Content-Length over MAX_BODY_KB  -> 413 PAYLOAD_TOO_LARGE
  - chunked body (no Content-Length) -> 411 (its size could only be checked by reading it)
  Without this, a client with no key at all could make us buffer and JSON-decode a body of any size before the 401.
uvicorn enforces that the body cannot exceed the declared Content-Length, so the size checks are sound."""

from starlette.types import ASGIApp, Receive, Scope, Send

from app.config import settings
from app.core.auth import is_known_api_key
from app.core.errors import ErrorCode, send_error

UPLOAD_PATHS = {"/api/v1/documents"}
BODY_METHODS = {"POST", "PUT", "PATCH"}
MULTIPART_OVERHEAD_BYTES = 1024 * 1024


class Rejected(Exception):
    def __init__(self, status: int, code: ErrorCode, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


class UploadGuardMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["method"] in BODY_METHODS and scope["path"].startswith("/api/"):
            headers = dict(scope.get("headers") or [])
            try:
                if scope["method"] == "POST" and scope["path"].rstrip("/") in UPLOAD_PATHS:
                    await _check_upload(headers)
                else:
                    _check_body_size(headers)
            except Rejected as r:
                return await send_error(send, r.status, r.code, r.message, close_connection=True)
        await self.app(scope, receive, send)


def _content_length(headers: dict[bytes, bytes]) -> int | None:
    raw = headers.get(b"content-length")
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        raise Rejected(400, ErrorCode.VALIDATION_ERROR, "Invalid Content-Length header.") from None


async def _check_upload(headers: dict[bytes, bytes]) -> None:
    api_key = headers.get(b"x-api-key")
    if not api_key:
        raise Rejected(401, ErrorCode.UNAUTHENTICATED, "Missing X-API-Key header.")
    length = _content_length(headers)
    if length is None:
        raise Rejected(411, ErrorCode.LENGTH_REQUIRED, "Content-Length header is required.")
    if length > settings.max_upload_bytes + MULTIPART_OVERHEAD_BYTES:
        raise Rejected(413, ErrorCode.FILE_TOO_LARGE, f"File exceeds the {settings.MAX_UPLOAD_MB} MB limit.")
    # latin-1 is how Starlette decodes headers, so this is the same string get_current_user will hash.
    if not await is_known_api_key(api_key.decode("latin-1")):
        raise Rejected(401, ErrorCode.UNAUTHENTICATED, "Invalid API key.")


def _check_body_size(headers: dict[bytes, bytes]) -> None:
    length = _content_length(headers)
    if length is None and b"transfer-encoding" in headers:
        raise Rejected(411, ErrorCode.LENGTH_REQUIRED, "Content-Length header is required.")
    if length is not None and length > settings.MAX_BODY_KB * 1024:
        raise Rejected(413, ErrorCode.PAYLOAD_TOO_LARGE, f"Request body exceeds the {settings.MAX_BODY_KB} KB limit.")
    # Neither header: the request has no body (e.g. POST /documents/{id}/reprocess).
