"""Cross-tenant route walker: every API route that takes a {document_id} or {session_id} must answer 404 when the
resource belongs to another user, whichever package wrote the route.

The walker reads app.openapi(), so routes merged later (insights, summaries, chat, ...) are covered without editing
this file. For each such route it creates the resources as `demo`, calls the route as `other` with a minimal valid
request built from the route's own OpenAPI schema, and expects 404 *_NOT_FOUND (never 403: that would confirm the id
exists). A positive control then calls the read-only routes as the owner and expects anything but 404, which proves
the requests were well formed, so the 404s really come from the ownership check and not from a bad request."""

import asyncio
import uuid
import warnings
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import pytest
from httpx import AsyncClient, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import DEMO, OTHER, upload

API_PREFIX = "/api/v1"
OWNED_PARAMS = {"document_id", "session_id"}
HTTP_METHODS = {"get", "post", "put", "patch", "delete"}
PUBLIC_API_ROUTES: set[tuple[str, str]] = set()   # (METHOD, path) of /api routes that are public on purpose: none
CALL_TIMEOUT_S = 10   # a 401/404 is instant; only an opened stream runs this long (ASGITransport buffers bodies)


@dataclass(frozen=True)
class Operation:
    method: str
    path: str                  # OpenAPI template, e.g. /api/v1/documents/{document_id}/pages/{page_no}
    spec: dict[str, Any]       # the OpenAPI operation object

    def __str__(self) -> str:
        return f"{self.method} {self.path}"

    def params(self, location: str) -> list[dict[str, Any]]:
        return [p for p in self.spec.get("parameters", []) if p["in"] == location]

    @property
    def path_param_names(self) -> set[str]:
        return {p["name"] for p in self.params("path")}

    @property
    def is_stream(self) -> bool:
        return "text/event-stream" in self.spec.get("responses", {}).get("200", {}).get("content", {})


def api_operations(spec: dict[str, Any]) -> list[Operation]:
    return [Operation(method.upper(), path, op)
            for path, ops in spec["paths"].items() if path.startswith(API_PREFIX)
            for method, op in ops.items() if method in HTTP_METHODS]


# --- minimal valid values from JSON Schema -------------------------------------------------------------------------
def _resolve(schema: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
    while "$ref" in schema:
        node: Any = spec
        for part in schema["$ref"].removeprefix("#/").split("/"):
            node = node[part]
        schema = node
    return schema


def example(schema: dict[str, Any], spec: dict[str, Any]) -> Any:
    """The smallest value the schema accepts: required fields only, defaults and first enum values preferred."""
    schema = _resolve(schema, spec)
    for key in ("default", "const"):
        if key in schema:
            return schema[key]
    if "enum" in schema:
        return schema["enum"][0]
    for key in ("anyOf", "oneOf", "allOf"):
        if key in schema:
            options = [o for o in schema[key] if _resolve(o, spec).get("type") != "null"]
            return example(options[0], spec) if options else None
    kind = schema.get("type")
    if kind == "object" or "properties" in schema:
        props = schema.get("properties", {})
        return {name: example(props[name], spec) for name in schema.get("required", [])}
    if kind == "array":
        return [example(schema.get("items", {}), spec) for _ in range(schema.get("minItems", 0))]
    if kind in ("integer", "number"):
        low = schema.get("minimum", schema.get("exclusiveMinimum", 0) + 1)
        return max(1, low)
    if kind == "boolean":
        return False
    if kind == "string":
        fmt = schema.get("format")
        if fmt == "uuid":
            return str(uuid.uuid4())
        if fmt in ("date", "date-time"):
            return "2026-01-01" if fmt == "date" else "2026-01-01T00:00:00Z"
        return "walker test"[: schema.get("maxLength", 100)].ljust(schema.get("minLength", 0), "x")
    return None


def _is_file(schema: dict[str, Any]) -> bool:
    return schema.get("format") == "binary" or "contentMediaType" in schema


def build_request(op: Operation, spec: dict[str, Any], ids: dict[str, str]) -> dict[str, Any]:
    """httpx request kwargs: owned ids in the path, schema examples for everything else."""
    url = op.path
    for p in op.params("path"):
        value = ids.get(p["name"]) or example(p.get("schema", {}), spec)
        url = url.replace("{" + p["name"] + "}", str(value))
    kwargs: dict[str, Any] = {
        "method": op.method, "url": url,
        "params": {p["name"]: example(p.get("schema", {}), spec) for p in op.params("query") if p.get("required")},
    }
    content = op.spec.get("requestBody", {}).get("content", {})
    if "application/json" in content:
        kwargs["json"] = example(content["application/json"]["schema"], spec)
    elif "multipart/form-data" in content:
        body = _resolve(content["multipart/form-data"]["schema"], spec)
        props = body.get("properties", {})
        required = body.get("required", [])
        kwargs["files"] = {k: ("walker.txt", b"walker test file", "text/plain") for k in required if _is_file(props[k])}
        kwargs["data"] = {k: str(example(props[k], spec)) for k in required if not _is_file(props[k])}
    return kwargs


async def call(client: AsyncClient, op: Operation, spec: dict[str, Any], ids: dict[str, str],
               headers: dict[str, str]) -> Response | None:
    """None when no complete response arrives within CALL_TIMEOUT_S, which in practice means a stream was opened."""
    try:
        async with asyncio.timeout(CALL_TIMEOUT_S):
            return await client.request(headers=headers, **build_request(op, spec, ids))
    except TimeoutError:
        return None


def describe(r: Response | None) -> str:
    return f"no response within {CALL_TIMEOUT_S} s (stream opened?)" if r is None else f"{r.status_code} {r.text[:200]}"


def error_code(r: Response) -> str:
    """The envelope's error.code, or "" for anything else (a success body, a file, an event stream)."""
    try:
        body = r.json()
    except ValueError:
        return ""
    error = body.get("error") if isinstance(body, dict) else None
    return error.get("code", "") if isinstance(error, dict) else ""


# --- fixtures ------------------------------------------------------------------------------------------------------
@pytest.fixture
def spec() -> dict[str, Any]:
    from app.main import app

    return app.openapi()


@pytest.fixture
async def generous_rate_limits(db: AsyncSession) -> AsyncIterator[None]:
    """The walker fires many requests in a few milliseconds; lift per-minute limits so it measures tenancy, not the
    rate limiter (whose guards run before the ownership check). Restored afterwards: users are shared state."""
    originals = (await db.execute(text("SELECT name, rate_limit_per_min FROM users"))).tuples().all()
    await db.execute(text("UPDATE users SET rate_limit_per_min = 100000"))
    await db.commit()
    yield
    for name, limit in originals:
        await db.execute(text("UPDATE users SET rate_limit_per_min = :n WHERE name = :u"), {"n": limit, "u": name})
    await db.commit()


async def create_owned_resources(client: AsyncClient, spec: dict[str, Any]) -> dict[str, str]:
    """One resource per owned path parameter, all belonging to `demo`."""
    r = await upload(client, "tenant-walker.txt", b"Walker document owned by demo. Annual leave is 18 days.")
    assert r.status_code == 202, r.text
    ids = {"document_id": r.json()["document"]["id"]}
    if "post" in spec["paths"].get(f"{API_PREFIX}/chat/sessions", {}):
        s = await client.post(f"{API_PREFIX}/chat/sessions", headers=DEMO, json={"document_ids": [ids["document_id"]]})
        assert s.status_code in (200, 201), f"walker could not create a chat session: {s.status_code} {s.text}"
        ids["session_id"] = s.json()["id"]
    return ids


# --- tests ---------------------------------------------------------------------------------------------------------
async def test_other_tenant_gets_404_on_every_owned_route(client: AsyncClient, spec: dict[str, Any],
                                                          generous_rate_limits: None) -> None:
    owned_routes = [op for op in api_operations(spec) if op.path_param_names & OWNED_PARAMS]
    owned_routes.sort(key=lambda op: op.method == "DELETE")   # a leaky DELETE must not hide leaks in later routes
    assert any("{document_id}" in op.path for op in owned_routes), "walker found no document routes to check"
    ids = await create_owned_resources(client, spec)

    walked, skipped, leaks = [], [], []
    for op in owned_routes:
        if not (op.path_param_names & OWNED_PARAMS) <= ids.keys():
            skipped.append(op)   # e.g. a {session_id} route on a branch without POST /chat/sessions
            continue
        r = await call(client, op, spec, ids, OTHER)
        walked.append(op)
        if r is None or r.status_code != 404 or not error_code(r).endswith("NOT_FOUND"):
            leaks.append(f"{op} as another tenant -> {describe(r)}")
    assert not leaks, "cross-tenant access:\n" + "\n".join(leaks)
    if skipped:
        warnings.warn(f"tenancy walker skipped (no way to create the resource): {', '.join(map(str, skipped))}",
                      stacklevel=1)

    # Positive control: as the owner, read-only routes whose every path parameter we control must NOT be 404.
    controllable = OWNED_PARAMS | {"page_no"}
    for op in walked:
        if op.method == "GET" and not op.is_stream and op.path_param_names <= controllable:
            r = await call(client, op, spec, ids, DEMO)
            assert r is None or r.status_code != 404, f"{op} is 404 for its owner too: the check above proves nothing"

    # Nothing the other tenant tried (DELETE, reprocess, ...) touched the owner's data.
    assert (await client.get(f"{API_PREFIX}/documents/{ids['document_id']}", headers=DEMO)).status_code == 200


async def test_every_api_route_requires_an_api_key(client: AsyncClient, spec: dict[str, Any]) -> None:
    unauthenticated = []
    for op in api_operations(spec):
        if (op.method, op.path) in PUBLIC_API_ROUTES:
            continue
        r = await call(client, op, spec, ids={}, headers={})
        if r is None or r.status_code != 401:
            unauthenticated.append(f"{op} without X-API-Key -> {describe(r)}")
    assert not unauthenticated, "\n".join(unauthenticated)


def test_example_builds_minimal_valid_values() -> None:
    spec = {"components": {"schemas": {"Ask": {
        "type": "object", "required": ["content", "ids"],
        "properties": {"content": {"type": "string", "minLength": 1}, "ids": {"type": "array", "minItems": 1,
                       "items": {"type": "string", "format": "uuid"}}, "title": {"type": "string"}}}}}}
    value = example({"$ref": "#/components/schemas/Ask"}, spec)
    assert set(value) == {"content", "ids"} and len(value["ids"]) == 1
    uuid.UUID(value["ids"][0])
    assert example({"anyOf": [{"type": "null"}, {"type": "integer", "minimum": 5}]}, spec) == 5
    assert example({"type": "string", "enum": ["short", "long"]}, spec) == "short"
