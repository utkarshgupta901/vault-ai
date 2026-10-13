"""End-to-end smoke test against a RUNNING stack (make smoke). Exits non-zero with a clear message on the first
failure.

    uv run python scripts/smoke.py [--api http://localhost:8000] [--key vault-demo-key]

Steps:
  1. /health/ready answers 200 (degraded dependencies are printed as warnings).
  2. Upload a small, unique TXT -> 202, then poll until it is ready.
  3. Upload the same bytes again -> 200 with duplicate: true and the same id (dedup by sha256).
  4. Upload without an API key -> 401 UNAUTHENTICATED (rejected by the upload guard before the body is read).
  5. If the chat API is deployed: create a session over the smoke document, stream a question and assert the SSE
     contract order meta -> token+ -> citations -> [followups] -> done (no error event), then check that the stored
     messages come back in seq order.
  6. If the metrics API is deployed: /metrics/documents and /metrics/ai-usage return non-zero counts.
The smoke document is always deleted at the end, so repeated runs never pollute the user's vault (or its chats)."""

import argparse
import sys
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

from evals.api_client import API_PREFIX, ApiError, VaultClient  # noqa: E402

GREEN, RED, YELLOW, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[0m"
KNOWN_EVENTS = {"meta", "token", "citations", "followups", "done", "error"}


class SmokeFailure(Exception):
    pass


def check(condition: bool, message: str) -> None:
    if not condition:
        raise SmokeFailure(message)


def event_order_problem(events: list[str]) -> str | None:
    """Validate the chat SSE contract (app/schemas/chat.py). Unknown event types are ignored, as clients must.
    Returns a description of the first problem, or None when the order is valid."""
    seen = [e for e in events if e in KNOWN_EVENTS]
    if "error" in seen:
        return "stream sent an error event"
    if not seen or seen[0] != "meta":
        return f"first event must be meta, got {seen[:1]}"
    if seen[-1] != "done" or seen.count("done") != 1:
        return "stream must end with exactly one done event"
    if "token" not in seen:
        return "no token events"
    if seen.count("citations") != 1:
        return f"expected exactly one citations event, got {seen.count('citations')}"
    if seen.index("citations") < max(i for i, e in enumerate(seen) if e == "token"):
        return "citations arrived before the last token"
    if "followups" in seen and not (seen.index("citations") < seen.index("followups") < seen.index("done")):
        return "followups must come after citations and before done"
    return None


# ---------------------------------------------------------------------------------------------------------------
class Smoke:
    def __init__(self, client: VaultClient, anonymous: VaultClient):
        self.client, self.anonymous = client, anonymous
        self.doc_id: str | None = None
        self.content = (f"Vault smoke test {uuid.uuid4()}.\nThe smoke test code word is HELIOTROPE.\n"
                        "This document exists only for a few seconds while the smoke test runs.\n").encode()

    def health(self) -> str:
        r = self.client.request("GET", "/health/ready", ok=(200, 503))
        body = r.json()
        check(r.status_code == 200, f"/health/ready returned {r.status_code}: {body.get('checks')}")
        degraded = [name for name, c in (body.get("checks") or {}).items() if not c.get("ok")]
        return f"status={body.get('status')}" + (f" {YELLOW}(degraded: {', '.join(degraded)}){RESET}"
                                                  if degraded else "")

    def upload(self) -> str:
        status, body = self.client.upload_bytes("smoke-test.txt", self.content)
        check(status == 202, f"first upload should return 202, got {status} (duplicate={body.get('duplicate')})")
        self.doc_id = body["document"]["id"]
        return f"202 document {self.doc_id}"

    def ready(self) -> str:
        assert self.doc_id
        started = time.monotonic()
        doc = self.client.wait_until_processed([self.doc_id], timeout_s=120, poll_s=0.5)[self.doc_id]
        check(doc["status"] == "ready", f"document did not become ready: status={doc['status']} "
                                        f"stage={doc.get('stage')} error={doc.get('error')}")
        return f"ready in {time.monotonic() - started:.1f}s ({doc.get('chunk_count')} chunks)"

    def duplicate(self) -> str:
        status, body = self.client.upload_bytes("smoke-test-again.txt", self.content)
        check(status == 200 and body.get("duplicate") is True, f"re-upload should be 200 duplicate, got {status}")
        check(body["document"]["id"] == self.doc_id, "duplicate returned a different document id")
        return "200 duplicate:true, same id"

    def no_key(self) -> str:
        try:
            self.anonymous.upload_bytes("smoke-test.txt", self.content)
        except ApiError as exc:
            check(exc.status == 401 and exc.code == "UNAUTHENTICATED", f"expected 401 UNAUTHENTICATED, got {exc}")
            return "401 UNAUTHENTICATED"
        raise SmokeFailure("upload without an API key was accepted")

    def chat(self) -> str:
        assert self.doc_id
        session = self.client.create_session([self.doc_id], title="smoke test")
        events = [event for event, _ in self.client.stream(session["id"], "What is the smoke test code word?")]
        problem = event_order_problem(events)
        check(problem is None, f"SSE contract violated: {problem}; events={events}")
        messages = self.client.messages(session["id"])
        seqs = [m["seq"] for m in messages]
        check(len(messages) >= 2 and seqs == sorted(seqs), f"messages not returned in seq order: {seqs}")
        check([m["role"] for m in messages[-2:]] == ["user", "assistant"], "last two messages are not user/assistant")
        tokens = sum(1 for e in events if e == "token")
        return f"stream ok ({tokens} tokens, events: {' '.join(dict.fromkeys(events))}); {len(messages)} messages"

    def metrics(self) -> str:
        docs = self.client.get_json(f"{API_PREFIX}/metrics/documents")
        usage = self.client.get_json(f"{API_PREFIX}/metrics/ai-usage")
        n_docs, n_calls = docs["totals"]["documents"], usage["totals"]["calls"]
        check(n_docs >= 1, f"/metrics/documents reports {n_docs} documents")
        check(n_calls >= 1, f"/metrics/ai-usage reports {n_calls} calls")
        return f"documents={n_docs} ai_calls={n_calls} today=${usage.get('today_spend_usd')}"

    def cleanup(self) -> str:
        if not self.doc_id:
            return "nothing to delete"
        self.client.request("DELETE", f"{API_PREFIX}/documents/{self.doc_id}", ok=(204, 404))
        return f"deleted {self.doc_id}"


def run_step(name: str, fn: Callable[[], str]) -> None:
    print(f"  {name:<10} ", end="", flush=True)
    try:
        detail = fn()
    except (SmokeFailure, ApiError, httpx.HTTPError, KeyError) as exc:
        print(f"{RED}FAIL{RESET} {exc}")
        raise SmokeFailure(f"{name}: {exc}") from exc
    print(f"{GREEN}ok{RESET}   {detail}")


def main(argv: list[str] | None = None, *, transport: Any = None) -> int:
    p = argparse.ArgumentParser(description="End-to-end smoke test against the running API.")
    p.add_argument("--api", default="http://localhost:8000")
    p.add_argument("--key", default="vault-demo-key")
    args = p.parse_args(argv)
    print(f"smoke test against {args.api}")
    with VaultClient(args.api, args.key, transport=transport) as client, \
            VaultClient(args.api, None, transport=transport) as anonymous:
        smoke = Smoke(client, anonymous)
        try:
            run_step("health", smoke.health)
            run_step("upload", smoke.upload)
            run_step("ready", smoke.ready)
            run_step("duplicate", smoke.duplicate)
            run_step("no-key", smoke.no_key)
            if client.has_route(f"{API_PREFIX}/chat/sessions"):
                run_step("chat", smoke.chat)
            else:
                print(f"  chat       {YELLOW}skip{RESET} chat API not deployed")
            if client.has_route(f"{API_PREFIX}/metrics/ai-usage"):
                run_step("metrics", smoke.metrics)
            else:
                print(f"  metrics    {YELLOW}skip{RESET} metrics API not deployed")
        except SmokeFailure as exc:
            print(f"{RED}SMOKE FAILED{RESET}: {exc}")
            return 1
        finally:
            try:
                run_step("cleanup", smoke.cleanup)
            except SmokeFailure:
                pass
    print(f"{GREEN}SMOKE PASSED{RESET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
