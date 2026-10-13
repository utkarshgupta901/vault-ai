"""A small synchronous client for the Vault HTTP API, shared by the eval runner and the ops scripts
(seed_demo, smoke, doctor, freeze_starters).

Why HTTP and not app code: these tools must behave exactly like an external client (X-API-Key header, JSON with
Content-Type, multipart uploads, SSE), so a passing smoke/eval run proves the API works, not just the services. Sync
httpx keeps the scripts linear and easy to read; nothing here needs concurrency."""

import json
import mimetypes
import time
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from typing import Any

import httpx

API_PREFIX = "/api/v1"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
MIME_BY_SUFFIX = {".pdf": "application/pdf", ".docx": DOCX_MIME, ".txt": "text/plain", ".md": "text/markdown"}


class ApiError(Exception):
    """Non-2xx response, carrying the API's error envelope code when there is one."""

    def __init__(self, method: str, path: str, status: int, code: str | None, message: str):
        super().__init__(f"{method} {path} -> {status} {code or ''} {message}".strip())
        self.status, self.code, self.message, self.path = status, code, message, path

    @classmethod
    def from_response(cls, r: httpx.Response) -> "ApiError":
        code, message = None, r.text[:300]
        try:
            err = r.json().get("error") or {}
            code, message = err.get("code"), err.get("message", message)
        except (ValueError, AttributeError):
            pass
        return cls(r.request.method, r.request.url.path, r.status_code, code, message)


def is_terminal(doc: dict[str, Any], *, need_insights: bool) -> bool:
    """Processing is over when the document failed, or is ready (and, if asked, its insights finished either way)."""
    if doc.get("status") == "failed":
        return True
    if doc.get("status") != "ready":
        return False
    return not need_insights or doc.get("insights_status") in ("ready", "failed")


def parse_sse(lines: Iterable[str]) -> Iterator[tuple[str, Any]]:
    """Turn Server-Sent Events lines into (event, data) pairs. A blank line ends an event; `data:` lines are joined
    and JSON-decoded when possible; comments (':' keep-alives) are skipped. Unnamed events are called 'message'."""
    event, data = "message", []
    for line in lines:
        line = line.rstrip("\r")
        if not line:
            if data:
                raw = "\n".join(data)
                try:
                    yield event, json.loads(raw)
                except ValueError:
                    yield event, raw
            event, data = "message", []
        elif line.startswith(":"):
            continue
        elif line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].removeprefix(" "))
    if data:
        yield event, "\n".join(data)


class VaultClient:
    def __init__(self, api: str, key: str | None, *, timeout: float = 90.0,
                 transport: httpx.BaseTransport | None = None):
        headers = {"X-API-Key": key} if key else {}
        self.api = api.rstrip("/")
        self.http = httpx.Client(base_url=self.api, headers=headers, timeout=timeout, transport=transport)
        self._paths: set[str] | None = None

    def close(self) -> None:
        self.http.close()

    def __enter__(self) -> "VaultClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # --- plumbing ------------------------------------------------------------------------------------------------
    def request(self, method: str, path: str, *, ok: tuple[int, ...] = (200,), **kwargs: Any) -> httpx.Response:
        r = self.http.request(method, path, **kwargs)
        if r.status_code not in ok:
            raise ApiError.from_response(r)
        return r

    def get_json(self, path: str, params: Any = None) -> Any:
        return self.request("GET", path, params=params).json()

    def post_json(self, path: str, body: dict[str, Any], *, ok: tuple[int, ...] = (200, 201)) -> Any:
        return self.request("POST", path, json=body, ok=ok).json()   # json= also sets Content-Type

    def routes(self) -> set[str]:
        """Paths in /openapi.json. Lets scripts skip endpoints that a work package hasn't shipped yet."""
        if self._paths is None:
            self._paths = set(self.get_json("/openapi.json").get("paths", {}))
        return self._paths

    def has_route(self, path: str) -> bool:
        return path in self.routes()

    # --- documents -----------------------------------------------------------------------------------------------
    def list_documents(self, page_size: int = 100) -> list[dict[str, Any]]:
        docs, cursor = [], None
        while True:
            params = {"limit": page_size, **({"cursor": cursor} if cursor else {})}
            page = self.get_json(f"{API_PREFIX}/documents", params)
            docs += page["items"]
            cursor = page.get("next_cursor")
            if not cursor:
                return docs

    def get_document(self, document_id: str) -> dict[str, Any]:
        return self.get_json(f"{API_PREFIX}/documents/{document_id}")

    def upload(self, path: Path) -> tuple[int, dict[str, Any]]:
        """POST the file as multipart. Returns (status, body): 202 = new, 200 = duplicate of an existing document."""
        return self.upload_bytes(path.name, path.read_bytes())

    def upload_bytes(self, filename: str, data: bytes, *, ok: tuple[int, ...] = (200, 202)) -> tuple[int, Any]:
        suffix = Path(filename).suffix.lower()
        mime = MIME_BY_SUFFIX.get(suffix) or mimetypes.guess_type(filename)[0] or "application/octet-stream"
        files = {"file": (filename, data, mime)}   # bytes (not a stream), so httpx sends a Content-Length
        r = self.request("POST", f"{API_PREFIX}/documents", files=files, ok=ok)
        return r.status_code, r.json()

    def wait_until_processed(self, document_ids: Iterable[str], *, need_insights: bool = False,
                             timeout_s: float = 600, poll_s: float = 1.0,
                             on_change: Callable[[dict[str, Any]], None] | None = None) -> dict[str, dict[str, Any]]:
        """Poll GET /documents/{id} until every document is terminal (see is_terminal) or the timeout passes.
        Returns the last state of each document; the caller decides what 'failed' or 'still processing' means."""
        pending = list(dict.fromkeys(document_ids))
        latest: dict[str, dict[str, Any]] = {}
        deadline = time.monotonic() + timeout_s
        while pending:
            for doc_id in list(pending):
                doc = self.get_document(doc_id)
                previous = latest.get(doc_id)
                if on_change and (previous is None or _status_key(previous) != _status_key(doc)):
                    on_change(doc)
                latest[doc_id] = doc
                if is_terminal(doc, need_insights=need_insights):
                    pending.remove(doc_id)
            if pending and time.monotonic() > deadline:
                break
            if pending:
                time.sleep(poll_s)
        return latest

    def ensure_uploaded(self, paths: Iterable[Path], *, need_insights: bool = False, timeout_s: float = 600,
                        poll_s: float = 1.0, on_change: Callable[[dict[str, Any]], None] | None = None,
                        ) -> dict[str, dict[str, Any]]:
        """Upload each file (duplicates are free: same bytes -> 200 + the existing document) and wait until all are
        processed. Returns {local filename: document} with `duplicate` added, keyed by the LOCAL name so a corpus
        file first uploaded under another name still resolves."""
        uploaded: dict[str, tuple[str, bool]] = {}
        for path in paths:
            status, body = self.upload(path)
            uploaded[path.name] = (body["document"]["id"], status == 200)
        final = self.wait_until_processed([doc_id for doc_id, _ in uploaded.values()], need_insights=need_insights,
                                          timeout_s=timeout_s, poll_s=poll_s, on_change=on_change)
        return {name: {**final[doc_id], "duplicate": dup} for name, (doc_id, dup) in uploaded.items()}

    # --- chat ----------------------------------------------------------------------------------------------------
    def create_session(self, document_ids: list[str] | None, title: str | None = None) -> dict[str, Any]:
        """document_ids=None means an all-documents session."""
        body: dict[str, Any] = {"all_documents": True} if document_ids is None else {"document_ids": document_ids}
        if title:
            body["title"] = title
        return self.post_json(f"{API_PREFIX}/chat/sessions", body)

    def ask(self, session_id: str, content: str) -> dict[str, Any]:
        """Non-streaming ask -> AskResponse {user_message, answer}."""
        return self.post_json(f"{API_PREFIX}/chat/sessions/{session_id}/messages", {"content": content})

    def stream(self, session_id: str, content: str) -> Iterator[tuple[str, Any]]:
        """Streaming ask: yields SSE (event, data) pairs as they arrive."""
        url = f"{API_PREFIX}/chat/sessions/{session_id}/messages/stream"
        with self.http.stream("POST", url, json={"content": content},
                              headers={"Accept": "text/event-stream"}) as r:
            if r.status_code != 200:
                r.read()
                raise ApiError.from_response(r)
            yield from parse_sse(r.iter_lines())

    def messages(self, session_id: str) -> list[dict[str, Any]]:
        body = self.get_json(f"{API_PREFIX}/chat/sessions/{session_id}/messages")
        return body["items"] if isinstance(body, dict) else body

    # --- search --------------------------------------------------------------------------------------------------
    def search(self, q: str, *, document_ids: list[str] | None, mode: str, k: int) -> dict[str, Any]:
        """GET /search. document_ids are sent as repeated query params (?document_ids=a&document_ids=b)."""
        params: list[tuple[str, str | int]] = [("q", q), ("mode", mode), ("k", k)]
        params += [("document_ids", d) for d in document_ids or []]
        return self.get_json(f"{API_PREFIX}/search", params)


def _status_key(doc: dict[str, Any]) -> tuple[Any, ...]:
    return doc.get("status"), doc.get("stage"), doc.get("insights_status")
