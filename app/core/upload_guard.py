"""Reject bad uploads BEFORE the body is read.

FastAPI parses multipart bodies (spooling the file to disk) before route dependencies run, so auth/size checks inside
the route would come after an anonymous client already made us write 25 MB to disk. This pure-ASGI guard runs first:
  - no X-API-Key header        -> 401 (validity is checked later; presence is enough to stop anonymous floods)
  - no Content-Length          -> 411 (chunked uploads can't be size-checked up front)
  - Content-Length too large   -> 413
uvicorn enforces that the body cannot exceed the declared Content-Length, so the size check is sound."""

import json

from starlette.types import ASGIApp, Receive, Scope, Send

from app.config import settings
from app.core.errors import ErrorCode
from app.core.request_context import get_request_id

UPLOAD_PATHS = {"/api/v1/documents", "/api/v1/documents/batch"}


class UploadGuardMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["method"] == "POST" and scope["path"].rstrip("/") in UPLOAD_PATHS:
            headers = dict(scope.get("headers") or [])
            limit = settings.max_upload_bytes + 1024 * 1024  # + multipart overhead
            if not headers.get(b"x-api-key"):
                return await _reject(send, 401, ErrorCode.UNAUTHENTICATED, "Missing X-API-Key header.")
            raw_length = headers.get(b"content-length")
            if raw_length is None:
                return await _reject(send, 411, ErrorCode.LENGTH_REQUIRED, "Content-Length header is required.")
            try:
                length = int(raw_length)
            except ValueError:
                return await _reject(send, 400, ErrorCode.VALIDATION_ERROR, "Invalid Content-Length header.")
            if length > limit:
                return await _reject(send, 413, ErrorCode.FILE_TOO_LARGE,
                                     f"File exceeds the {settings.MAX_UPLOAD_MB} MB limit.")
        await self.app(scope, receive, send)


async def _reject(send: Send, status: int, code: str, message: str) -> None:
    body = json.dumps(
        {"error": {"code": code, "message": message, "details": {}, "request_id": get_request_id()}}
    ).encode()
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()),
                            (b"connection", b"close")]})
    await send({"type": "http.response.body", "body": body})
