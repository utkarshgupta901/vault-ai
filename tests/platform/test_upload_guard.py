"""UploadGuard rejects before a single body byte is read: unknown keys on uploads, oversized or chunked bodies on the
other /api write routes. The tests drive the middleware directly with a receive() that counts reads."""

from typing import Any

import pytest
from httpx import AsyncClient

from app.config import settings
from app.core import auth
from app.core.upload_guard import UploadGuardMiddleware
from tests.conftest import DEMO


class Probe:
    """Inner app + receive() that record whether the request got past the guard and whether the body was read."""

    def __init__(self) -> None:
        self.app_called = False
        self.body_reads = 0
        self.sent: list[dict[str, Any]] = []

    async def app(self, scope: Any, receive: Any, send: Any) -> None:
        self.app_called = True
        await receive()
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def receive(self) -> dict[str, Any]:
        self.body_reads += 1
        return {"type": "http.request", "body": b"x", "more_body": False}

    async def send(self, message: dict[str, Any]) -> None:
        self.sent.append(message)

    @property
    def status(self) -> int:
        return self.sent[0]["status"]

    async def call(self, method: str, path: str, headers: dict[str, str]) -> "Probe":
        scope = {"type": "http", "method": method, "path": path,
                 "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()]}
        await UploadGuardMiddleware(self.app)(scope, self.receive, self.send)
        return self


UPLOAD_SIZE = str(20 * 1024 * 1024)


async def test_bogus_key_is_rejected_before_the_upload_body(db: Any) -> None:
    p = await Probe().call("POST", "/api/v1/documents", {"X-API-Key": "made-up", "Content-Length": UPLOAD_SIZE})
    assert (p.status, p.body_reads, p.app_called) == (401, 0, False)


async def test_valid_key_passes_and_is_cached(db: Any) -> None:
    auth._known_key_hashes.clear()
    p = await Probe().call("POST", "/api/v1/documents",
                           {"X-API-Key": settings.SEED_DEMO_API_KEY, "Content-Length": UPLOAD_SIZE})
    assert (p.status, p.app_called) == (204, True)
    assert auth.hash_api_key(settings.SEED_DEMO_API_KEY) in auth._known_key_hashes
    assert auth.hash_api_key("made-up") not in auth._known_key_hashes      # only valid keys are cached


async def test_oversized_upload_is_rejected_without_a_db_lookup() -> None:
    too_big = str(settings.max_upload_bytes + 2 * 1024 * 1024)
    p = await Probe().call("POST", "/api/v1/documents", {"X-API-Key": "made-up", "Content-Length": too_big})
    assert (p.status, p.body_reads) == (413, 0)     # no `db` fixture: a DB lookup here would fail the test


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH"])
async def test_large_json_body_is_rejected_before_it_is_read(method: str) -> None:
    length = str(settings.MAX_BODY_KB * 1024 + 1)
    p = await Probe().call(method, "/api/v1/chat/sessions", {"Content-Length": length})   # no key at all
    assert (p.status, p.body_reads, p.app_called) == (413, 0, False)


async def test_only_the_real_upload_route_gets_the_upload_limit() -> None:
    # There is no batch upload route: a 20 MB body sent to a path under /documents gets the JSON body limit.
    p = await Probe().call("POST", "/api/v1/documents/batch", {"Content-Length": UPLOAD_SIZE})
    assert (p.status, p.body_reads, p.app_called) == (413, 0, False)


async def test_chunked_json_body_needs_a_length() -> None:
    p = await Probe().call("POST", "/api/v1/documents/compare", {"Transfer-Encoding": "chunked"})
    assert (p.status, p.body_reads) == (411, 0)


@pytest.mark.parametrize(("method", "path", "headers"), [
    ("POST", "/api/v1/chat/sessions", {"Content-Length": "120"}),          # normal JSON body
    ("POST", "/api/v1/documents/x/reprocess", {}),                         # no body at all
    ("GET", "/api/v1/documents", {"Content-Length": str(10 * 1024 * 1024)}),
    ("POST", "/not-api", {"Content-Length": str(10 * 1024 * 1024)}),
])
async def test_other_requests_pass_through(method: str, path: str, headers: dict[str, str]) -> None:
    p = await Probe().call(method, path, headers)
    assert (p.status, p.app_called) == (204, True)


async def test_envelope_through_the_real_app(client: AsyncClient) -> None:
    r = await client.post("/api/v1/chat/sessions", content=b"x" * (settings.MAX_BODY_KB * 1024 + 1),
                          headers={"Content-Type": "application/json", "X-Request-ID": "big-body"})
    assert r.status_code == 413
    assert r.json()["error"] == {"code": "PAYLOAD_TOO_LARGE", "message": "Request body exceeds the 1024 KB limit.",
                                 "details": {}, "request_id": "big-body"}
    bogus = await client.post("/api/v1/documents", headers={"X-API-Key": "made-up"},
                              files={"file": ("a.txt", b"hello", "text/plain")})
    assert (bogus.status_code, bogus.json()["error"]["message"]) == (401, "Invalid API key.")
    ok = await client.post("/api/v1/chat/sessions", headers=DEMO, json={"document_ids": []})
    assert ok.status_code != 413
