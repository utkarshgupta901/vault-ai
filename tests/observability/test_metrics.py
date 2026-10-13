"""Metrics APIs computed over fixture rows inserted with SQL, so every expected number can be worked out by hand.

percentile_cont interpolates linearly: for sorted values v0..vn-1 the p-th percentile sits at position p*(n-1).
E.g. [100, 500, 1000, 3000]: p50 at 1.5 -> 750; p95 at 2.85 -> 1000 + 0.85 * 2000 = 2700."""

import uuid
from collections import Counter
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.metrics import WINDOWS
from tests.conftest import DEMO, OTHER
from tests.observability.conftest import (
    add_document,
    add_message,
    add_request_log,
    add_run,
    add_session,
    add_usage,
    ago,
)

ZERO_PCT = {"p50": None, "p95": None, "avg": None}


async def get(client: AsyncClient, path: str, headers: dict = DEMO) -> dict:
    r = await client.get(f"/api/v1/metrics/{path}", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


# --- /metrics/documents --------------------------------------------------------------------------------------------
async def test_document_metrics(client: AsyncClient, db: AsyncSession, demo_user_id: uuid.UUID) -> None:
    recent, old = ago(seconds=60), ago(days=3)
    await add_document(db, demo_user_id, size_bytes=1000, page_count=2, chunk_count=4, token_count=100,
                       upload_count=3, category="policy", tags=["hr", "pto"], created_at=recent,
                       ready_at=recent + timedelta(seconds=10))
    await add_document(db, demo_user_id, mime_type="application/pdf", size_bytes=5000, page_count=10, chunk_count=20,
                       token_count=900, category="contract", tags=["legal", "hr"], created_at=recent,
                       ready_at=recent + timedelta(seconds=30))
    await add_document(db, demo_user_id, mime_type="application/pdf", size_bytes=200, status="failed",
                       insights_status="pending", created_at=old)
    await add_document(db, demo_user_id, status="processing", insights_status="pending", created_at=recent)
    await db.commit()

    body = await get(client, "documents")

    assert body["totals"] == {"documents": 4, "pages": 12, "chunks": 24, "bytes": 6300, "tokens": 1000,
                              "dedup_hits": 2}
    assert body["by_status"] == {"queued": 0, "processing": 1, "ready": 2, "failed": 1}
    assert body["by_insights_status"] == {"pending": 2, "processing": 0, "ready": 2, "failed": 0}
    assert body["by_mime_type"] == {"text/plain": 2, "application/pdf": 2}
    assert body["by_category"] == {"policy": 1, "contract": 1, "uncategorized": 2}
    assert body["top_tags"] == [{"key": "hr", "count": 2}, {"key": "legal", "count": 1}, {"key": "pto", "count": 1}]
    # Ready after 10 s and 30 s: p50 20, p95 at position 0.95 -> 10 + 0.95 * 20 = 29.
    assert body["seconds_to_ready"] == {"p50": 20.0, "p95": 29.0, "avg": 20.0}

    days = body["uploads_per_day"]
    assert len(days) == 14
    assert days[-1]["day"] == datetime.now(UTC).date().isoformat()   # oldest first, ends today (UTC)
    expected = Counter(ts.astimezone(UTC).date().isoformat() for ts in (recent, recent, old, recent))
    assert {d["day"]: d["count"] for d in days if d["count"]} == expected


# --- /metrics/processing -------------------------------------------------------------------------------------------
async def test_processing_metrics(client: AsyncClient, db: AsyncSession, demo_user_id: uuid.UUID) -> None:
    broken = await add_document(db, demo_user_id, filename="broken.pdf", status="failed")
    await add_document(db, demo_user_id, status="processing", insights_status="pending",
                       created_at=ago(minutes=40), updated_at=ago(minutes=30))           # in flight AND stale
    await add_document(db, demo_user_id, status="queued", insights_status="pending")    # in flight, fresh
    await add_run(db, demo_user_id, duration_ms=1000, stage_ms={"extract": 100, "embed": 800}, started_at=ago(hours=1))
    await add_run(db, demo_user_id, duration_ms=3000, stage_ms={"extract": 300, "embed": 2000, "chunk": 50},
                  started_at=ago(hours=2))
    await add_run(db, demo_user_id, status="retrying", error_code="PROVIDER_UNAVAILABLE", duration_ms=500,
                  stage_ms={"extract": 200, "note": "slow"})                            # non-numeric key ignored
    await add_run(db, demo_user_id, status="failed", error_code="CORRUPT_FILE", error_message="bad xref",
                  document_id=broken, duration_ms=100, finished_at=ago(minutes=3))
    await add_run(db, demo_user_id, task="insights", status="failed", error_code="WORKER_LOST",
                  finished_at=ago(minutes=1))                                          # crashed, no duration
    await add_run(db, demo_user_id, duration_ms=9999, stage_ms={"extract": 9999}, started_at=ago(days=2))  # outside 24h
    await db.commit()

    body = await get(client, "processing?window=24h")

    assert body["window"] == "24h"
    assert {k: body[k] for k in ("attempts", "succeeded", "failed", "retried", "crashed")} == {
        "attempts": 5, "succeeded": 2, "failed": 2, "retried": 1, "crashed": 1}
    assert body["success_rate"] == 0.5
    assert body["duration_ms"] == {"ingest": {"p50": 750.0, "p95": 2700.0, "avg": 1150.0}}
    assert body["stage_ms"] == {
        "chunk": {"p50": 50.0, "p95": 50.0, "avg": 50.0},
        "embed": {"p50": 1400.0, "p95": 1940.0, "avg": 1400.0},
        "extract": {"p50": 200.0, "p95": 290.0, "avg": 200.0},
    }
    assert body["failures_by_code"] == [{"key": "CORRUPT_FILE", "count": 1}, {"key": "WORKER_LOST", "count": 1}]
    newest, older = body["recent_failures"]
    assert (newest["task"], newest["error_code"], newest["document_id"]) == ("insights", "WORKER_LOST", None)
    assert (older["document_id"], older["filename"], older["error_message"]) == (str(broken), "broken.pdf", "bad xref")
    assert (body["in_flight"], body["stale"]) == (2, 1)
    assert set(body["queue_depth"]) == {"ingest", "ai"}
    assert all(v is None or isinstance(v, int) for v in body["queue_depth"].values())
    hours = body["throughput"]
    assert len(hours) == 25 and all(h["hour"].endswith(":00:00Z") for h in hours)
    assert sum(h["succeeded"] for h in hours) == 2 and sum(h["failed"] for h in hours) == 2

    assert (await get(client, "processing?window=7d"))["attempts"] == 6     # the 2-day-old attempt is back in


async def test_queue_depth_is_none_when_broker_is_unreachable(
    client: AsyncClient, db: AsyncSession, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import redis

    from app.services import metrics

    monkeypatch.setattr(metrics, "get_sync_broker",
                        lambda: redis.from_url("redis://localhost:1/0", socket_connect_timeout=0.2))
    body = await get(client, "processing")
    assert body["queue_depth"] == {"ingest": None, "ai": None}


# --- /metrics/ai-usage ---------------------------------------------------------------------------------------------
async def _seed_usage(db: AsyncSession, owner: uuid.UUID) -> None:
    doc = uuid.uuid4()   # ai_usage has no foreign keys: cost history survives document deletion
    await add_usage(db, owner, "embed_chunks", model="text-embedding-3-small", input_tokens=1000,
                    cost_usd=Decimal("0.00002"), latency_ms=100, document_id=doc)
    await add_usage(db, owner, "insights", input_tokens=2000, output_tokens=500, reasoning_tokens=100,
                    cost_usd=Decimal("0.00045"), latency_ms=2000, document_id=doc)
    await add_usage(db, owner, "embed_query", model="text-embedding-3-small", cache_hit=True, latency_ms=0)
    await add_usage(db, owner, "answer", model="gpt-6.1-sol", input_tokens=3000, cached_input_tokens=1000,
                    output_tokens=200, cost_usd=Decimal("0.0062"), latency_ms=1500, estimated=True)
    await add_usage(db, owner, "answer", model="gpt-6.1-sol", success=False, latency_ms=300)
    await add_usage(db, owner, "condense", input_tokens=100, output_tokens=20, cost_usd=Decimal("0.0001"),
                    latency_ms=400)
    await add_usage(db, owner, "answer", model="gpt-6.1-sol", cost_usd=Decimal("1.0"), created_at=ago(days=2))
    session = await add_session(db, owner)
    await add_message(db, session, role="user", content="q")
    await add_message(db, session, answer_type="answered")
    await add_message(db, session, answer_type="not_found")


async def test_ai_usage_metrics_by_operation(client: AsyncClient, db: AsyncSession, demo_user_id: uuid.UUID) -> None:
    await _seed_usage(db, demo_user_id)
    await db.commit()

    body = await get(client, "ai-usage?window=24h&group_by=operation")

    totals = body["totals"]
    assert {k: totals[k] for k in ("calls", "input_tokens", "cached_input_tokens", "output_tokens",
                                   "reasoning_tokens")} == {
        "calls": 6, "input_tokens": 6100, "cached_input_tokens": 1000, "output_tokens": 720, "reasoning_tokens": 100}
    assert totals["cost_usd"] == pytest.approx(0.00677)
    assert (totals["cache_hit_rate"], totals["estimated_share"], totals["error_rate"]) == (0.1667, 0.1667, 0.1667)

    groups = {g["key"]: g for g in body["groups"]}
    assert [g["key"] for g in body["groups"]] == ["answer", "insights", "condense", "embed_chunks", "embed_query"]
    assert groups["answer"] | {"cost_usd": 0} == {"key": "answer", "calls": 2, "input_tokens": 3000,
                                                  "output_tokens": 200, "cost_usd": 0, "avg_latency_ms": 900.0}
    assert groups["answer"]["cost_usd"] == pytest.approx(0.0062)
    # (embed_chunks + insights) over the 1 document they were spent on.
    assert body["cost_per_document_usd"] == pytest.approx(0.00047)
    # (answer + condense + embed_query + followups) over 2 assistant messages; the user message is not a turn.
    assert body["cost_per_answer_usd"] == pytest.approx(0.00315)
    assert body["today_spend_usd"] == pytest.approx(0.00677)          # the 2-day-old $1 is not today
    assert body["daily_limit_usd"] == 20.0


async def test_ai_usage_metrics_by_model_and_day(client: AsyncClient, db: AsyncSession,
                                                 demo_user_id: uuid.UUID) -> None:
    await _seed_usage(db, demo_user_id)
    await db.commit()

    by_model = await get(client, "ai-usage?window=7d&group_by=model")
    assert by_model["group_by"] == "model"
    assert [(g["key"], g["calls"]) for g in by_model["groups"]] == [
        ("gpt-6.1-sol", 3), ("gpt-6-luna", 2), ("text-embedding-3-small", 2)]
    assert by_model["totals"]["cost_usd"] == pytest.approx(1.00677)

    by_day = await get(client, "ai-usage?window=7d&group_by=day")
    days = [g["key"] for g in by_day["groups"]]
    assert days == sorted(days) and len(days) == 2
    assert days[-1] == datetime.now(UTC).date().isoformat()
    assert sum(g["calls"] for g in by_day["groups"]) == 7


# --- /metrics/chat -------------------------------------------------------------------------------------------------
async def test_chat_metrics(client: AsyncClient, db: AsyncSession, demo_user_id: uuid.UUID) -> None:
    s = await add_session(db, demo_user_id)
    await add_message(db, s, role="user", content="q")                                   # user rows are ignored
    await add_message(db, s, answer_type="answered", confidence="high", ttft_ms=900, latency_ms=2000,
                      citations=[{"ref": "S1"}, {"ref": "S2"}, {"ref": "S3"}],
                      timings={"condense": 100, "embed": 50, "retrieve": 30, "ttft": 900, "total": 2000},
                      signals={"gate": False, "invalid_citations": 1, "uncited_claims": 0})
    await add_message(db, s, answer_type="answered", confidence="medium", citations=[{"ref": "S1"}],
                      timings={"embed": 70, "retrieve": 40, "ttft": 1100, "total": 3000, "rerank": 12},
                      signals={"invalid_citations": 0, "uncited_claims": 2}, warnings=["fallback_model"])
    await add_message(db, s, answer_type="not_found", confidence="low", signals={"gate": True},
                      timings={"embed": 60, "retrieve": 20, "total": 100})
    await add_message(db, s, answer_type="not_found", confidence="low", signals={"gate": False})
    await add_message(db, s, status="failed")
    await add_message(db, s, status="interrupted")
    await add_message(db, s, answer_type="answered", confidence="high", created_at=ago(days=8))  # outside 7d
    await db.commit()

    body = await get(client, "chat?window=7d")

    assert body["answers"] == 6
    # The second answer has no ttft_ms/latency_ms columns, so the timings JSON is used instead.
    assert body["ttft_ms"] == {"p50": 1000.0, "p95": 1090.0, "avg": 1000.0}
    assert body["latency_ms"] == {"p50": 2000.0, "p95": 2900.0, "avg": 1700.0}
    assert body["stage_ms"] == {
        "condense": {"p50": 100.0, "p95": 100.0, "avg": 100.0},
        "embed": {"p50": 60.0, "p95": 69.0, "avg": 60.0},
        "rerank": {"p50": 12.0, "p95": 12.0, "avg": 12.0},
        "retrieve": {"p50": 30.0, "p95": 39.0, "avg": 30.0},
    }
    assert body["answer_types"] == {"answered": 2, "not_found": 2, "ungrounded": 0}
    assert body["not_found_by_path"] == {"gate": 1, "model": 1}
    assert body["confidence"] == {"high": 1, "medium": 1, "low": 2}
    assert body["avg_citations"] == 2.0                    # answered rows only: (3 + 1) / 2
    assert (body["invalid_citations"], body["uncited_claims"], body["fallback_model"]) == (1, 2, 1)
    assert (body["errors"], body["interrupted"]) == (1, 1)


# --- /metrics/system -----------------------------------------------------------------------------------------------
async def test_system_metrics_per_route(client: AsyncClient, db: AsyncSession, quiet_request_logs: None) -> None:
    for ttfb in (10, 20, 30):
        await add_request_log(db, "GET", "/api/v1/documents", 200, ttfb)
    await add_request_log(db, "GET", "/api/v1/documents/{document_id}", 404, 5)
    await add_request_log(db, "GET", "/api/v1/documents/{document_id}", 200, 15)
    await add_request_log(db, "POST", "/api/v1/documents", 500, 100)
    await add_request_log(db, "GET", "/api/v1/documents", 200, 999, created_at=ago(hours=2))   # outside 1h
    await db.commit()

    body = await get(client, "system?window=1h")

    assert body["requests"] == 6
    routes = {(r["method"], r["route"]): r for r in body["routes"]}
    assert body["routes"][0]["route"] == "/api/v1/documents"            # busiest route first
    assert routes[("GET", "/api/v1/documents")] == {
        "method": "GET", "route": "/api/v1/documents", "count": 3,
        "ttfb_ms": {"p50": 20.0, "p95": 29.0, "avg": 20.0}, "error_4xx_rate": 0.0, "error_5xx_rate": 0.0}
    assert routes[("GET", "/api/v1/documents/{document_id}")]["error_4xx_rate"] == 0.5
    assert routes[("POST", "/api/v1/documents")]["error_5xx_rate"] == 1.0

    # Global by design: another user sees the same per-route numbers (plus, possibly, the call above).
    other = await get(client, "system?window=1h", OTHER)
    other_routes = {(r["method"], r["route"]): r for r in other["routes"]}
    assert other_routes[("GET", "/api/v1/documents")] == routes[("GET", "/api/v1/documents")]


# --- scoping and validation ----------------------------------------------------------------------------------------
async def test_another_user_sees_only_zeros(client: AsyncClient, db: AsyncSession, demo_user_id: uuid.UUID) -> None:
    doc = await add_document(db, demo_user_id, status="processing", page_count=3, tags=["hr"],
                             updated_at=ago(minutes=30))
    await add_run(db, demo_user_id, document_id=doc, status="failed", error_code="CORRUPT_FILE", duration_ms=10)
    await _seed_usage(db, demo_user_id)
    s = await add_session(db, demo_user_id)
    await add_message(db, s, answer_type="answered", confidence="high", ttft_ms=100)
    await db.commit()

    docs = await get(client, "documents", OTHER)
    assert set(docs["totals"].values()) == {0}
    assert docs["top_tags"] == [] and docs["by_mime_type"] == {} and docs["by_category"] == {}
    assert sum(d["count"] for d in docs["uploads_per_day"]) == 0
    assert docs["seconds_to_ready"] == ZERO_PCT

    proc = await get(client, "processing?window=30d", OTHER)
    assert (proc["attempts"], proc["in_flight"], proc["stale"]) == (0, 0, 0)
    assert proc["failures_by_code"] == [] and proc["recent_failures"] == [] and proc["stage_ms"] == {}
    assert proc["success_rate"] is None

    usage = await get(client, "ai-usage?window=30d", OTHER)
    assert usage["totals"]["calls"] == 0 and usage["groups"] == []
    assert (usage["today_spend_usd"], usage["daily_limit_usd"]) == (0.0, 2.0)
    assert usage["cost_per_document_usd"] is None and usage["cost_per_answer_usd"] is None

    chat = await get(client, "chat?window=30d", OTHER)
    assert chat["answers"] == 0 and chat["ttft_ms"] == ZERO_PCT and set(chat["answer_types"].values()) == {0}

    # ...while the owner does see the same rows, so the zeros above are scoping, not an empty fixture.
    assert (await get(client, "documents"))["totals"]["documents"] == 1
    assert (await get(client, "processing?window=30d"))["attempts"] == 1
    assert (await get(client, "chat?window=30d"))["answers"] == 3


@pytest.mark.parametrize("path", [
    "processing?window=2h", "ai-usage?window=1y", "chat?window=", "system?window=24", "ai-usage?group_by=user",
])
async def test_invalid_window_or_group_by_is_422(client: AsyncClient, path: str) -> None:
    r = await client.get(f"/api/v1/metrics/{path}", headers=DEMO)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.parametrize("window", sorted(WINDOWS))
async def test_every_documented_window_is_accepted(client: AsyncClient, window: str) -> None:
    for path in ("processing", "ai-usage", "chat", "system"):
        assert (await get(client, f"{path}?window={window}"))["window"] == window


async def test_metrics_require_an_api_key(client: AsyncClient) -> None:
    for path in ("documents", "processing", "ai-usage", "chat", "system"):
        r = await client.get(f"/api/v1/metrics/{path}")
        assert r.status_code == 401, path
