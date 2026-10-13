"""An in-memory stand-in for the Vault HTTP API, used as an httpx.MockTransport handler.

It lets the eval runner and the ops scripts be tested end to end (HTTP shapes, polling, sessions, SSE parsing,
report writing) without the compose stack and without any model. It implements only what those clients call, with
the response shapes of app/schemas/*. Answers and search hits come from functions the test supplies."""

import hashlib
import json
import re
import uuid
from collections.abc import Callable
from typing import Any

import httpx

ALL_ROUTES = {"/api/v1/documents", "/api/v1/search", "/api/v1/chat/sessions", "/api/v1/metrics/ai-usage",
              "/api/v1/metrics/documents"}

AnswerFn = Callable[[str, list[str], list[str]], dict[str, Any]]      # (question, prior questions, scope ids)
SearchFn = Callable[[str, str, list[str]], list[dict[str, Any]]]      # (query, mode, document ids) -> hits


def envelope(status: int, code: str, message: str = "") -> httpx.Response:
    return httpx.Response(status, json={"error": {"code": code, "message": message or code, "details": {},
                                                  "request_id": None}})


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
        self.spend = 0.0
        self.calls: list[str] = []

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

    def list_documents(self, _: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": list(reversed(self.docs.values())), "next_cursor": None})

    def get_document(self, _: httpx.Request, doc_id: str) -> httpx.Response:
        doc = self.docs.get(doc_id)
        if doc is None:
            return envelope(404, "DOCUMENT_NOT_FOUND")
        self.polls[doc_id] += 1
        if self.polls[doc_id] >= self.polls_until_ready:
            doc.update(status="ready", insights_status="ready", page_count=1, chunk_count=1, category="other")
        return httpx.Response(200, json=doc)

    def delete_document(self, _: httpx.Request, doc_id: str) -> httpx.Response:
        return httpx.Response(204) if self.docs.pop(doc_id, None) else envelope(404, "DOCUMENT_NOT_FOUND")

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
        sid = str(uuid.uuid4())
        self.sessions[sid] = {"id": sid, "all_documents": body.get("all_documents", False),
                              "document_ids": body.get("document_ids", []), "starter_questions": []}
        self.questions[sid] = []
        return httpx.Response(201, json=self.sessions[sid])

    def _message(self, sid: str, role: str, seq: int, extra: dict[str, Any]) -> dict[str, Any]:
        return {"id": str(uuid.uuid4()), "seq": seq, "session_id": sid, "role": role, "status": "complete",
                "citations": [], "signals": {}, **extra}

    def ask(self, request: httpx.Request, sid: str) -> httpx.Response:
        question = json.loads(request.content)["content"]
        prior = list(self.questions[sid])
        self.questions[sid].append(question)
        answer = {"answer_type": "answered", "confidence": "medium", "model": "fake-answer", "prompt_version": "v2",
                  "ttft_ms": 100, "latency_ms": 900, "cost_usd": "0.010000",
                  **self.answer_fn(question, prior, self.sessions[sid]["document_ids"])}
        self.spend += float(answer.get("cost_usd") or 0)
        n = len(self.questions[sid]) * 2
        return httpx.Response(200, json={"user_message": self._message(sid, "user", n - 1, {"content": question}),
                                         "answer": self._message(sid, "assistant", n, answer)})

    def stream(self, request: httpx.Request, sid: str) -> httpx.Response:
        self.questions[sid].append(json.loads(request.content)["content"])
        body = "".join(f"event: {e}\ndata: {json.dumps({'delta': 'x'} if e == 'token' else {})}\n\n"
                       for e in self.events)
        return httpx.Response(200, content=body.encode(), headers={"content-type": "text/event-stream"})

    def messages(self, _: httpx.Request, sid: str) -> httpx.Response:
        items = []
        for i, q in enumerate(self.questions[sid]):
            items += [self._message(sid, "user", 2 * i + 1, {"content": q}),
                      self._message(sid, "assistant", 2 * i + 2, {"content": "ok"})]
        return httpx.Response(200, json={"items": items, "next_cursor": None})

    # --- metrics --------------------------------------------------------------------------------------------------
    def ai_usage(self, _: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"today_spend_usd": round(self.spend, 6), "daily_limit_usd": 50.0,
                                         "totals": {"calls": len(self.docs) + 1}})

    def doc_metrics(self, _: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"totals": {"documents": len(self.docs)}})


def transport(api: FakeVaultApi) -> httpx.MockTransport:
    return httpx.MockTransport(api)
