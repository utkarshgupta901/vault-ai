"""Pre-demo checklist (make doctor): one PASS / WARN / FAIL line per check, exit code 1 if anything FAILs.

    uv run python scripts/doctor.py [--api http://localhost:8000] [--key vault-demo-key]

Checks, in the order they matter on demo day:
  - the API is reachable (/health/live) and ready (/health/ready), with every dependency check listed; the AI check
    is soft (WARN), except that a key or model the provider rejects (auth_failed) FAILs: at least one AI feature is
    broken or degraded (a revoked key breaks them all; a missing answer model sends every answer to the fallback);
  - both Celery queues (ingest, ai) have a consumer: without them uploads queue forever;
  - the API key works;
  - the demo corpus (plus any real document in demo_docs/user/) is present, ready and has insights (pre-seeded by
    scripts/seed_demo.py);
  - the demo library holds ONLY the demo corpus: no other document, no pre-loaded live upload, and nothing that is
    only trusted because a file of that name sits in demo_docs/ (same rules as make pre-demo, which deletes the
    first two);
  - today's AI spend for this key is well under its daily quota (/metrics/ai-usage, when deployed);
  - the keyword leg ranks the LRK-4471 board-update chunk first (/search, when deployed);
  - one real embeddings call works: a vector-mode /search for a query unique to this run (the API's embedding cache
    cannot answer it), which must neither fail nor quietly fall back to keyword (costs a fraction of a cent);
  - which models the server is configured to use.
Each assessment is a pure function of an API response, so it is unit-tested without a server."""

import argparse
import os
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

from evals.api_client import API_PREFIX, ApiError, VaultClient  # noqa: E402
from evals.corpus import BOARD_UPDATE, DEMO_DOCS, SEED_FILES, user_files  # noqa: E402
from evals.demo_library import (  # noqa: E402
    DEMO_USER,
    KnownFile,
    known_files,
    plan_documents,
    user_docs_warning,
)

SPEND_WARN_USD = 1.0          # the demo checklist wants the demo user under $1 before going live
HARD_DEPENDENCIES = {"database"}


@dataclass(frozen=True)
class Check:
    level: str                # PASS | WARN | FAIL
    name: str
    detail: str


def assess_ready(status_code: int, body: dict[str, Any]) -> list[Check]:
    """Overall readiness plus one line per dependency. Hard dependencies FAIL, soft ones WARN (the API still serves)."""
    overall = body.get("status")
    level = "PASS" if status_code == 200 and overall == "ok" else ("WARN" if status_code == 200 else "FAIL")
    checks = [Check(level, "readiness", f"HTTP {status_code}, status={overall}")]
    for name, c in (body.get("checks") or {}).items():
        extra = {k: v for k, v in c.items() if k not in ("ok", "ms")}
        detail = f"{c.get('ms', '-')} ms" + (f" {extra}" if extra else "")
        # A rejected AI key/model: the API still serves (so readiness only says degraded), but an AI feature is broken.
        bad = "FAIL" if name in HARD_DEPENDENCIES or c.get("auth_failed") else "WARN"
        checks.append(Check("PASS" if c.get("ok") else bad, f"  {name}", detail))
    return checks


def assess_queues(body: dict[str, Any]) -> list[Check]:
    consumers = ((body.get("checks") or {}).get("workers") or {}).get("consumers") or {}
    return [Check("PASS" if consumers.get(q) else "FAIL", f"queue {q}",
                  f"{consumers.get(q, 0)} consumer(s)" + ("" if consumers.get(q) else " - start the worker"))
            for q in ("ingest", "ai")]


def assess_documents(docs: list[dict[str, Any]], expected: tuple[str, ...] = SEED_FILES) -> list[Check]:
    by_name: dict[str, dict[str, Any]] = {}
    for doc in docs:                                   # newest first: keep the newest copy of each filename
        by_name.setdefault(doc["filename"], doc)
    missing = [n for n in expected if n not in by_name]
    not_ready = [n for n in expected if n in by_name and by_name[n]["status"] != "ready"]
    no_insights = [n for n in expected if n in by_name and by_name[n].get("insights_status") != "ready"]
    ready = sum(1 for d in docs if d["status"] == "ready")
    with_insights = sum(1 for d in docs if d.get("insights_status") == "ready")
    checks = [Check("PASS" if not (missing or not_ready) else "FAIL", "demo corpus",
                    f"{len(expected) - len(missing) - len(not_ready)}/{len(expected)} seed files ready"
                    + (f"; missing {missing} (run make seed)" if missing else "")
                    + (f"; not ready {not_ready}" if not_ready else "")),
              Check("PASS" if not no_insights else "WARN", "insights",
                    f"{with_insights}/{len(docs)} documents have insights"
                    + (f"; pending/failed: {no_insights}" if no_insights else "")),
              Check("PASS", "documents", f"{len(docs)} total, {ready} ready")]
    failed = [d["filename"] for d in docs if d["status"] == "failed"]
    if failed:
        checks.append(Check("WARN", "failed documents", ", ".join(failed)))
    return checks


def assess_library(docs: list[dict[str, Any]], known: list[KnownFile], hint: str | None = None) -> Check:
    """The demo library holds only the demo corpus: the same rules as make pre-demo (evals/demo_library.py), so a
    FAIL here is exactly what `make pre-demo APPLY=1` would delete. hint: why the plan may be off (no user/ dir)."""
    decisions, warnings = plan_documents(DEMO_USER, docs, known, demo=True)
    unexpected = [d.filename for d in decisions if d.delete and d.role is None]
    live = [d.filename for d in decisions if d.delete and d.role is not None]
    untrusted = [d.filename for d in decisions if not d.delete and d.role == "extra"]
    if unexpected or live or untrusted:
        parts = ([f"unexpected documents: {', '.join(unexpected)}"] if unexpected else []) \
            + ([f"live uploads pre-loaded: {', '.join(live)}"] if live else []) \
            + ([f"only matched by a file in demo_docs/ that evals/corpus.py does not name: {', '.join(untrusted)}"]
               if untrusted else [])
        return Check("FAIL", "demo library", "; ".join(parts) + " -> make pre-demo"
                     + (f" ({hint})" if hint and unexpected else ""))
    if warnings:
        return Check("WARN", "demo library", "; ".join(w.removeprefix(f"{DEMO_USER}: ") for w in warnings))
    return Check("PASS", "demo library", f"only the demo corpus ({len(docs)} documents)")


def assess_spend(usage: dict[str, Any]) -> Check:
    spend, limit = float(usage.get("today_spend_usd") or 0), float(usage.get("daily_limit_usd") or 0)
    level = "FAIL" if limit and spend >= limit else ("WARN" if spend >= SPEND_WARN_USD else "PASS")
    return Check(level, "AI spend today", f"${spend:.4f} of ${limit:.2f} daily quota")


def assess_lrk_search(resp: dict[str, Any]) -> Check:
    hits = resp.get("hits") or []
    top = hits[0] if hits else {}
    ok = top.get("filename") == BOARD_UPDATE and "lrk-4471" in (top.get("content") or "").lower()
    return Check("PASS" if ok else "WARN", "keyword search",
                 f"LRK-4471 top hit: {top.get('filename')} {top.get('label')} lex_rank={top.get('lex_rank')}"
                 if hits else "LRK-4471 returned no hits")


def embedding_probe_query() -> str:
    """Unique to this run, so the API's query-embedding cache cannot answer it: the embeddings API is really called."""
    return f"doctor embedding probe {uuid.uuid4()}"


def assess_embedding_probe(resp: dict[str, Any]) -> Check:
    """When the query embedding fails, /search still answers 200 but drops the vector leg; `mode` (the legs that
    actually ran) is the only sign of it."""
    mode = resp.get("mode")
    if mode == "vector":
        return Check("PASS", "embedding probe", f"live embeddings call OK, top_similarity={resp.get('top_similarity')}")
    return Check("FAIL", "embedding probe", f"vector search ran as mode={mode}: the query embedding failed "
                 "(API key, model or quota; see the API log)")


def run_embedding_probe(client: VaultClient, docs: list[dict[str, Any]]) -> Check:
    if not any(d["status"] == "ready" for d in docs):
        return Check("WARN", "embedding probe", "skipped: no ready document (the API embeds a query only to search one)")
    try:
        resp = client.search(embedding_probe_query(), document_ids=None, mode="vector", k=1)
    except ApiError as exc:
        return Check("FAIL", "embedding probe", f"vector search failed: {exc}")
    return assess_embedding_probe(resp)


# ---------------------------------------------------------------------------------------------------------------
def colour(level: str) -> str:
    if not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
        return level
    code = {"PASS": "32", "WARN": "33", "FAIL": "31"}[level]
    return f"\033[{code}m{level}\033[0m"


def gather(client: VaultClient) -> list[Check]:
    checks: list[Check] = []
    try:
        client.request("GET", "/health/live")
        checks.append(Check("PASS", "API reachable", client.api))
    except (ApiError, httpx.HTTPError) as exc:
        return [Check("FAIL", "API reachable", f"{client.api}: {exc}")]
    r = client.request("GET", "/health/ready", ok=(200, 503))
    ready = r.json()
    checks += assess_ready(r.status_code, ready) + assess_queues(ready)
    try:
        docs = client.list_documents()
    except ApiError as exc:
        return checks + [Check("FAIL", "API key", str(exc))]
    checks.append(Check("PASS", "API key", "accepted"))
    checks += assess_documents(docs, SEED_FILES + tuple(p.name for p in user_files()))
    checks.append(assess_library(docs, known_files(), user_docs_warning(DEMO_DOCS)))
    if client.has_route(f"{API_PREFIX}/metrics/ai-usage"):
        checks.append(assess_spend(client.get_json(f"{API_PREFIX}/metrics/ai-usage", {"window": "24h"})))
    else:
        checks.append(Check("WARN", "AI spend today", "metrics API not deployed"))
    if client.has_route(f"{API_PREFIX}/search"):
        checks.append(assess_lrk_search(client.search("LRK-4471", document_ids=None, mode="keyword", k=5)))
        checks.append(run_embedding_probe(client, docs))
    chat = client.has_route(f"{API_PREFIX}/chat/sessions")
    checks.append(Check("PASS" if chat else "FAIL", "chat API", "deployed" if chat else "not in /openapi.json"))
    models = ready.get("models") or {}
    checks.append(Check("PASS", "models", ", ".join(f"{k}={v}" for k, v in models.items()) or "unknown"))
    return checks


def main(argv: list[str] | None = None, *, transport: Any = None) -> int:
    p = argparse.ArgumentParser(description="Pre-demo checklist.")
    p.add_argument("--api", default="http://localhost:8000")
    p.add_argument("--key", default="vault-demo-key")
    args = p.parse_args(argv)
    with VaultClient(args.api, args.key, timeout=15, transport=transport) as client:
        try:
            checks = gather(client)
        except (ApiError, httpx.HTTPError) as exc:
            checks = [Check("FAIL", "doctor", f"unexpected API error: {exc}")]
    for c in checks:
        print(f"{colour(c.level)}  {c.name:<18} {c.detail}")
    failures = sum(1 for c in checks if c.level == "FAIL")
    warnings = sum(1 for c in checks if c.level == "WARN")
    print(f"\n{failures} FAIL, {warnings} WARN, {len(checks) - failures - warnings} PASS")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
