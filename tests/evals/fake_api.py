"""An in-memory stand-in for the Vault HTTP API, used as an httpx.MockTransport handler.

It lets the eval runner and the ops scripts be tested end to end (HTTP shapes, polling, sessions, SSE parsing,
report writing) without the compose stack and without any model. It implements only what those clients call, with
the response shapes of app/schemas/*. Answers and search hits come from functions the test supplies.
Lists page like the real API (limit 1..100, an opaque next_cursor; messages: newest page first, each page oldest
first), so a client that ignores the cursor loses items here too.
One instance is one user's vault; users_transport() puts several behind one server, keyed by API key."""

import hashlib
import json
import re
import uuid
from collections.abc import Callable
from typing import Any

import httpx

ALL_ROUTES = {"/api/v1/documents", "/api/v1/search", "/api/v1/chat/sessions", "/api/v1/metrics/ai-usage",
              "/api/v1/metrics/documents"}

# What every real answer carries (app/schemas/chat.Signals, all keys always present). An answer_fn's own `signals`
# replaces it whole, so a test can drop or rename it to see the eval fail closed.
SIGNALS = {"top_similarity": 0.6, "id_hit": False, "lexical_match": True, "valid_citations": 0,
           "invalid_citations": 0, "uncited_claims": 0, "ungrounded_facts": 0, "gate": False}

AnswerFn = Callable[[str, list[str], list[str]], dict[str, Any]]      # (question, prior questions, scope ids)
SearchFn = Callable[[str, str, list[str]], list[dict[str, Any]]]      # (query, mode, document ids) -> hits


def envelope(status: int, code: str, message: str = "") -> httpx.Response:
    return httpx.Response(status, json={"error": {"code": code, "message": message or code, "details": {},
                                                  "request_id": None}})


def page_params(request: httpx.Request) -> tuple[int, int | None] | httpx.Response:
    """(limit, cursor position) of a list request, or the 422/400 the real API answers. Cursors here are positions
    in the list, which is enough for tests that never change a list while paging it."""
    params = request.url.params
    try:
        limit = int(params.get("limit", 20))
        cursor = int(params["cursor"]) if params.get("cursor") else None
    except ValueError:
        return envelope(400, "INVALID_CURSOR")
    if not 1 <= limit <= 100:
        return envelope(422, "VALIDATION_ERROR")
    return limit, cursor


def newest_first_page(request: httpx.Request, items: list[dict[str, Any]]) -> httpx.Response:
    """Documents and sessions: newest first, next_cursor = where the next page starts."""
    parsed = page_params(request)
    if isinstance(parsed, httpx.Response):
        return parsed
    limit, start = parsed[0], parsed[1] or 0
    newest = list(reversed(items))
    end = start + limit
    return httpx.Response(200, json={"items": newest[start:end],
                                     "next_cursor": str(end) if end < len(newest) else None})


class FakeVaultApi:
    def __init__(self, *, answer_fn: AnswerFn | None = None, search_fn: SearchFn | None = None,
                 routes: set[str] | None = None, polls_until_ready: int = 1,
                 events: list[str] | None = None):
        self.answer_fn = answer_fn or (lambda q, prior, ids: {"content": "I couldn't find this in your documents.",
                                                              "answer_type": "not_found"})
        self.search_fn = search_fn or (lambda q, mode, ids: [])
        self.routes = ALL_ROUTES if routes is None else routes
        self.polls_until_ready = polls_until_ready
        self.events = events or ["meta", "token", "token", "citations", "followups", "done"]
        self.docs: dict[str, dict[str, Any]] = {}
        self.contents: dict[str, bytes] = {}
        self.polls: dict[str, int] = {}
        self.sessions: dict[str, dict[str, Any]] = {}
        self.questions: dict[str, list[str]] = {}
        self.history: dict[str, list[dict[str, Any]]] = {}              # session id -> stored messages
        self.spend = 0.0
        self.calls: list[str] = []

    # --- test setup: state a script would find on a used stack ------------------------------------------------------
    def add_document(self, filename: str, data: bytes | None = None, **fields: Any) -> dict[str, Any]:
        """A ready document with insights, as if uploaded and processed long ago."""
        data = data if data is not None else f"content of {filename}".encode()
        doc_id = str(uuid.uuid4())
        self.docs[doc_id] = {"id": doc_id, "filename": filename, "sha256": hashlib.sha256(data).hexdigest(),
                             "status": "ready", "stage": None, "insights_status": "ready", "upload_count": 1,
                             "page_count": 1, "chunk_count": 1, "category": "other", "document_date": None,
                             "error": None, **fields}
        self.contents[doc_id] = data
        self.polls[doc_id] = 0
        return self.docs[doc_id]

    def add_session(self, title: str | None, document_ids: list[str] | None = None,
                    messages: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """document_ids=None: an all-documents session. messages: assistant/user rows (seq and ids are filled in)."""
        sid = str(uuid.uuid4())
        self.sessions[sid] = {"id": sid, "title": title, "all_documents": document_ids is None,
                              "document_ids": list(document_ids or []), "created_at": "2026-10-13T10:00:00Z",
                              "last_message_at": None, "starter_questions": [], "warnings": []}
        self.questions[sid] = []
        self.history[sid] = [self._message(sid, m.get("role", "assistant"), seq, m)
                             for seq, m in enumerate(messages or [], start=1)]
        return self.sessions[sid]

    # --- dispatch -------------------------------------------------------------------------------------------------
    def __call__(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        self.calls.append(f"{method} {path}")
        if path.startswith("/api/") and not request.headers.get("x-api-key"):
            return envelope(401, "UNAUTHENTICATED")
        routes: list[tuple[str, str, Callable[..., httpx.Response]]] = [
            ("GET", r"/openapi\.json", self.openapi),
            ("GET", r"/health/live", lambda r: httpx.Response(200, json={"status": "ok"})),
            ("GET", r"/health/ready", self.ready),
            ("POST", r"/api/v1/documents", self.upload),
            ("GET", r"/api/v1/documents", self.list_documents),
            ("GET", r"/api/v1/documents/(?P<doc_id>[^/]+)", self.get_document),
            ("DELETE", r"/api/v1/documents/(?P<doc_id>[^/]+)", self.delete_document),
            ("GET", r"/api/v1/documents/(?P<doc_id>[^/]+)/insights", self.insights),
            ("GET", r"/api/v1/search", self.search),
            ("POST", r"/api/v1/chat/sessions", self.create_session),
            ("GET", r"/api/v1/chat/sessions", self.list_sessions),
            ("GET", r"/api/v1/chat/sessions/(?P<sid>[^/]+)", self.get_session),
            ("DELETE", r"/api/v1/chat/sessions/(?P<sid>[^/]+)", self.delete_session),
            ("POST", r"/api/v1/chat/sessions/(?P<sid>[^/]+)/messages", self.ask),
            ("POST", r"/api/v1/chat/sessions/(?P<sid>[^/]+)/messages/stream", self.stream),
            ("GET", r"/api/v1/chat/sessions/(?P<sid>[^/]+)/messages", self.messages),
            ("GET", r"/api/v1/metrics/ai-usage", self.ai_usage),
            ("GET", r"/api/v1/metrics/documents", self.doc_metrics),
        ]
        for verb, pattern, handler in routes:
            m = re.fullmatch(pattern, path)
            if verb == method and m:
                route_root = re.sub(r"/\(\?P<\w+>\[\^/\]\+\).*", "", pattern).replace("\\", "")
                if route_root.startswith("/api/") and not any(r.startswith(route_root) for r in self.routes):
                    return envelope(404, "NOT_FOUND")
                return handler(request, **m.groupdict())
        return envelope(404, "NOT_FOUND")

    # --- meta -----------------------------------------------------------------------------------------------------
    def openapi(self, _: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"paths": {p: {} for p in self.routes}})

    def ready(self, _: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "status": "ok",
            "checks": {"database": {"ok": True, "ms": 3}, "redis": {"ok": True, "ms": 1},
                       "workers": {"ok": True, "ms": 5, "consumers": {"ingest": 1, "ai": 1}}},
            "models": {"answer": "fake-answer", "embeddings": "fake-embed"}})

    # --- documents ------------------------------------------------------------------------------------------------
    def upload(self, request: httpx.Request) -> httpx.Response:
        m = re.search(rb'filename="([^"]+)"\r\nContent-Type: [^\r]+\r\n\r\n(.*)\r\n--', request.content, re.S)
        assert m, "multipart body not understood"
        filename, data = m.group(1).decode(), m.group(2)
        sha = hashlib.sha256(data).hexdigest()
        for doc in self.docs.values():
            if doc["sha256"] == sha:
                doc["upload_count"] += 1
                return httpx.Response(200, json={"document": doc, "duplicate": True, "requeued": False})
        doc_id = str(uuid.uuid4())
        self.docs[doc_id] = {"id": doc_id, "filename": filename, "sha256": sha, "status": "queued", "stage": None,
                             "insights_status": "pending", "upload_count": 1, "page_count": None,
                             "chunk_count": None, "category": None, "document_date": None, "error": None}
        self.contents[doc_id] = data
        self.polls[doc_id] = 0
        return httpx.Response(202, json={"document": self.docs[doc_id], "duplicate": False, "requeued": False})

    def list_documents(self, request: httpx.Request) -> httpx.Response:
        return newest_first_page(request, list(self.docs.values()))

    def get_document(self, _: httpx.Request, doc_id: str) -> httpx.Response:
        doc = self.docs.get(doc_id)
        if doc is None:
            return envelope(404, "DOCUMENT_NOT_FOUND")
        self.polls[doc_id] += 1
        if self.polls[doc_id] >= self.polls_until_ready:
            doc.update(status="ready", insights_status="ready", page_count=1, chunk_count=1, category="other")
        return httpx.Response(200, json=doc)

    def delete_document(self, _: httpx.Request, doc_id: str) -> httpx.Response:
        """Like the real delete: scope rows cascade away, and cited snippets of the file are redacted (the citation
        itself, with its filename, stays)."""
        if not self.docs.pop(doc_id, None):
            return envelope(404, "DOCUMENT_NOT_FOUND")
        for session in self.sessions.values():
            session["document_ids"] = [d for d in session["document_ids"] if d != doc_id]
        for messages in self.history.values():
            for m in messages:
                m["citations"] = [{**{k: v for k, v in c.items() if k != "snippet"}, "source_deleted": True}
                                  if c.get("document_id") == doc_id else c for c in m["citations"]]
        return httpx.Response(204)

    def insights(self, _: httpx.Request, doc_id: str) -> httpx.Response:
        name = self.docs[doc_id]["filename"]
        return httpx.Response(200, json={"document_id": doc_id, "status": "ready",
                                         "insights": {"suggested_questions": [f"What is in {name}?",
                                                                              f"Who wrote {name}?"]}})

    # --- search ---------------------------------------------------------------------------------------------------
    def search(self, request: httpx.Request) -> httpx.Response:
        params = request.url.params
        k = int(params.get("k", 8))
        if k > 40:
            return envelope(422, "VALIDATION_ERROR")
        hits = self.search_fn(params["q"], params.get("mode", "hybrid"), params.get_list("document_ids"))[:k]
        return httpx.Response(200, json={"query": params["q"], "mode": params.get("mode"), "terms": [],
                                         "id_terms": [], "top_similarity": 0.5, "hits": hits})

    # --- chat -----------------------------------------------------------------------------------------------------
    def create_session(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        session = self.add_session(body.get("title"),
                                   None if body.get("all_documents") else body.get("document_ids", []))
        return httpx.Response(201, json=session)

    def list_sessions(self, request: httpx.Request) -> httpx.Response:
        return newest_first_page(request, list(self.sessions.values()))

    def get_session(self, _: httpx.Request, sid: str) -> httpx.Response:
        session = self.sessions.get(sid)
        return httpx.Response(200, json=session) if session else envelope(404, "SESSION_NOT_FOUND")

    def delete_session(self, _: httpx.Request, sid: str) -> httpx.Response:
        if not self.sessions.pop(sid, None):
            return envelope(404, "SESSION_NOT_FOUND")
        self.history.pop(sid, None)
        return httpx.Response(204)

    def _message(self, sid: str, role: str, seq: int, extra: dict[str, Any]) -> dict[str, Any]:
        return {"id": str(uuid.uuid4()), "seq": seq, "session_id": sid, "role": role, "status": "complete",
                "content": "", "citations": [], "signals": {}, "retrieval": None, **extra}

    def _append(self, sid: str, question: str, answer: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        n = len(self.history[sid])
        user_message = self._message(sid, "user", n + 1, {"content": question})
        pair = (user_message, self._message(sid, "assistant", n + 2, {"reply_to_id": user_message["id"], **answer}))
        self.history[sid] += pair
        self.sessions[sid]["last_message_at"] = "2026-10-13T12:00:00Z"
        return pair

    def ask(self, request: httpx.Request, sid: str) -> httpx.Response:
        question = json.loads(request.content)["content"]
        prior = list(self.questions[sid])
        self.questions[sid].append(question)
        reply = self.answer_fn(question, prior, self.sessions[sid]["document_ids"])
        answer = {"answer_type": "answered", "confidence": "medium", "model": "fake-answer", "prompt_version": "v2",
                  "ttft_ms": 100, "latency_ms": 900, "cost_usd": "0.010000",
                  "signals": {**SIGNALS, "valid_citations": len(reply.get("citations") or [])}, **reply}
        self.spend += float(answer.get("cost_usd") or 0)
        user_message, answer_message = self._append(sid, question, answer)
        return httpx.Response(200, json={"user_message": user_message, "answer": answer_message})

    def stream(self, request: httpx.Request, sid: str) -> httpx.Response:
        question = json.loads(request.content)["content"]
        self.questions[sid].append(question)
        self._append(sid, question, {"content": "ok"})
        body = "".join(f"event: {e}\ndata: {json.dumps({'delta': 'x'} if e == 'token' else {})}\n\n"
                       for e in self.events)
        return httpx.Response(200, content=body.encode(), headers={"content-type": "text/event-stream"})

    def messages(self, request: httpx.Request, sid: str) -> httpx.Response:
        """Like the real API: the newest `limit` messages, oldest first within the page; next_cursor = older ones."""
        if sid not in self.sessions:
            return envelope(404, "SESSION_NOT_FOUND")
        parsed = page_params(request)
        if isinstance(parsed, httpx.Response):
            return parsed
        history = self.history[sid]
        limit, end = parsed[0], len(history) if parsed[1] is None else parsed[1]
        start = max(0, end - limit)
        return httpx.Response(200, json={"items": history[start:end], "next_cursor": str(start) if start else None})

    # --- metrics --------------------------------------------------------------------------------------------------
    def ai_usage(self, _: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"today_spend_usd": round(self.spend, 6), "daily_limit_usd": 50.0,
                                         "totals": {"calls": len(self.docs) + 1}})

    def doc_metrics(self, _: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"totals": {"documents": len(self.docs)}})


def transport(api: FakeVaultApi) -> httpx.MockTransport:
    return httpx.MockTransport(api)


def users_transport(apis: dict[str, FakeVaultApi]) -> httpx.MockTransport:
    """Several users behind one server: each API key has its own vault, and an unknown key gets 401 (like the real
    API). Unauthenticated routes (/health, /openapi.json) go to the first user's fake."""
    def handler(request: httpx.Request) -> httpx.Response:
        api = apis.get(request.headers.get("x-api-key") or "")
        if api is None:
            if request.url.path.startswith("/api/"):
                return envelope(401, "UNAUTHENTICATED")
            api = next(iter(apis.values()))
        return api(request)
    return httpx.MockTransport(handler)
