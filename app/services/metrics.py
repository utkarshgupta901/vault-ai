"""Metrics behind /api/v1/metrics/* (response shapes are frozen in app/schemas/metrics.py).

Everything is computed on read with plain SQL over tables the system already writes:
  documents        -> what the user has (counts, sizes, tags, time to ready)
  processing_runs  -> how the pipeline behaves (one row per attempt: retries, failures, stage timings)
  ai_usage         -> what the AI costs (one row per model call)
  chat_messages    -> how answers behave (assistant rows: timings, signals, warnings, answer_type, confidence)
  request_logs     -> how the API performs (written by RequestLogMiddleware)
Percentiles are Postgres `percentile_cont`, so raw rows never travel to Python. Every query filters by owner_id,
except /metrics/system and the queue depth, which describe the shared system rather than one user's data.

Plain SQL (not ORM expressions) on purpose: these are reporting queries and each number in the dashboard should be
traceable to one readable statement. SQL text is built only from constants in this module; request values (owner,
window start) always travel as bind parameters. Days and hours are UTC.

At demo scale these scans take milliseconds thanks to the (owner_id, created_at), (started_at) and (created_at)
indexes. At larger scale the answer is hourly rollup tables or a metrics store.
"""

import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal

import anyio
import structlog
from sqlalchemy import TextClause, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.ratelimit import today_spend_usd
from app.core.redis import get_sync_broker
from app.schemas.metrics import (
    AIUsageMetrics,
    ChatMetrics,
    CountByKey,
    DayCount,
    DocumentStats,
    DocumentTotals,
    HourBucket,
    Percentiles,
    ProcessingMetrics,
    RecentFailure,
    RouteStats,
    SystemMetrics,
    UsageGroup,
    UsageTotals,
)

log = structlog.get_logger(__name__)

Window = Literal["15m", "1h", "6h", "24h", "7d", "30d"]
GroupBy = Literal["operation", "model", "day"]
WINDOWS: dict[str, timedelta] = {
    "15m": timedelta(minutes=15), "1h": timedelta(hours=1), "6h": timedelta(hours=6),
    "24h": timedelta(hours=24), "7d": timedelta(days=7), "30d": timedelta(days=30),
}

QUEUES = ("ingest", "ai")              # Celery queues = Redis lists of the same name on the broker
QUEUE_DEPTH_TIMEOUT_S = 1.5
UPLOAD_DAYS = 14
TOP_TAGS = 10
RECENT_FAILURES = 10
MAX_ROUTES = 100
# Known enum values are always present (as 0) so dashboards and clients never need "missing key" checks.
DOCUMENT_STATUSES = ("queued", "processing", "ready", "failed")
INSIGHTS_STATUSES = ("pending", "processing", "ready", "failed")
ANSWER_TYPES = ("answered", "not_found", "ungrounded")
CONFIDENCE_LEVELS = ("high", "medium", "low")


def window_start(window: str, now: datetime | None = None) -> datetime:
    return (now or datetime.now(UTC)) - WINDOWS[window]


def _pct_sql(expr: str, prefix: str = "") -> str:
    """SQL for p50 / p95 / avg of one expression (NULLs are ignored by all three)."""
    return (f"percentile_cont(0.5) WITHIN GROUP (ORDER BY {expr}) AS {prefix}p50, "
            f"percentile_cont(0.95) WITHIN GROUP (ORDER BY {expr}) AS {prefix}p95, "
            f"avg({expr}) AS {prefix}avg")


def _num(value: Any, digits: int = 1) -> float | None:
    return None if value is None else round(float(value), digits)


def _percentiles(row: Mapping[str, Any], prefix: str = "") -> Percentiles:
    return Percentiles(p50=_num(row[f"{prefix}p50"]), p95=_num(row[f"{prefix}p95"]), avg=_num(row[f"{prefix}avg"]))


def _ratio(part: int, whole: int) -> float | None:
    return round(part / whole, 4) if whole else None


# --- GET /metrics/documents ------------------------------------------------------------------------------------
_DOC_TOTALS = text(f"""
SELECT count(*)                           AS documents,
       coalesce(sum(page_count), 0)       AS pages,
       coalesce(sum(chunk_count), 0)      AS chunks,
       coalesce(sum(size_bytes), 0)       AS bytes,
       coalesce(sum(token_count), 0)      AS tokens,
       coalesce(sum(upload_count - 1), 0) AS dedup_hits,
       {_pct_sql("EXTRACT(EPOCH FROM ready_at - created_at)")}
FROM documents
WHERE owner_id = :owner_id
""")

_DOC_BREAKDOWNS = text("""
SELECT 'status' AS dim, status AS key, count(*) AS n FROM documents WHERE owner_id = :owner_id GROUP BY status
UNION ALL
SELECT 'insights_status', insights_status, count(*) FROM documents WHERE owner_id = :owner_id GROUP BY insights_status
UNION ALL
SELECT 'mime_type', mime_type, count(*) FROM documents WHERE owner_id = :owner_id GROUP BY mime_type
UNION ALL
SELECT 'category', coalesce(category, 'uncategorized'), count(*) FROM documents WHERE owner_id = :owner_id
GROUP BY category
""")

_DOC_TOP_TAGS = text("""
SELECT tag AS key, count(*) AS count
FROM documents CROSS JOIN LATERAL unnest(tags) AS tag
WHERE owner_id = :owner_id
GROUP BY tag
ORDER BY count(*) DESC, tag
LIMIT :limit
""")

# generate_series gives one row per day, so days without uploads show up as 0 instead of being missing.
_DOC_UPLOADS_PER_DAY = text("""
SELECT CAST(g.day AS date) AS day, count(d.id) AS count
FROM generate_series(CAST(:first_day AS date), CAST(:last_day AS date), interval '1 day') AS g(day)
LEFT JOIN documents d
       ON d.owner_id = :owner_id AND CAST(d.created_at AT TIME ZONE 'UTC' AS date) = CAST(g.day AS date)
GROUP BY g.day
ORDER BY g.day
""")


async def document_stats(db: AsyncSession, owner_id: uuid.UUID) -> DocumentStats:
    """All-time totals for the caller's documents (no window: this is an inventory, not a rate)."""
    params = {"owner_id": owner_id}
    totals = (await db.execute(_DOC_TOTALS, params)).mappings().one()

    breakdowns: dict[str, dict[str, int]] = {
        "status": dict.fromkeys(DOCUMENT_STATUSES, 0), "insights_status": dict.fromkeys(INSIGHTS_STATUSES, 0),
        "mime_type": {}, "category": {},
    }
    for row in (await db.execute(_DOC_BREAKDOWNS, params)).mappings():
        breakdowns[row["dim"]][row["key"]] = row["n"]

    tags = (await db.execute(_DOC_TOP_TAGS, params | {"limit": TOP_TAGS})).mappings()
    today = datetime.now(UTC).date()
    days = (await db.execute(_DOC_UPLOADS_PER_DAY, params | {
        "first_day": today - timedelta(days=UPLOAD_DAYS - 1), "last_day": today,
    })).mappings()

    return DocumentStats(
        totals=DocumentTotals(**{k: int(totals[k]) for k in DocumentTotals.model_fields}),
        by_status=breakdowns["status"],
        by_insights_status=breakdowns["insights_status"],
        by_mime_type=breakdowns["mime_type"],
        by_category=breakdowns["category"],
        top_tags=[CountByKey(key=r["key"], count=r["count"]) for r in tags],
        uploads_per_day=[DayCount(day=r["day"].isoformat(), count=r["count"]) for r in days],
        seconds_to_ready=_percentiles(totals),
    )


# --- GET /metrics/processing -------------------------------------------------------------------------------------
_RUN_COUNTS = text("""
SELECT count(*)                                       AS attempts,
       count(*) FILTER (WHERE status = 'succeeded')   AS succeeded,
       count(*) FILTER (WHERE status = 'failed' AND error_code IS DISTINCT FROM 'SUPERSEDED') AS failed,  -- a reprocess taking over is not a failure
       count(*) FILTER (WHERE status = 'retrying')    AS retried,
       count(*) FILTER (WHERE error_code = 'WORKER_LOST') AS crashed
FROM processing_runs
WHERE owner_id = :owner_id AND started_at >= :since
""")

_RUN_DURATIONS = text(f"""
SELECT task AS key, {_pct_sql("duration_ms")}
FROM processing_runs
WHERE owner_id = :owner_id AND started_at >= :since AND duration_ms IS NOT NULL
GROUP BY task
ORDER BY task
""")

# stage_ms is a JSON object with whatever stages the pipeline timed ({"download": 120, "extract": 900, ...});
# jsonb_each turns each key into a row, so new stages appear in the metrics without code changes.
_RUN_STAGES = text(f"""
SELECT s.key, {_pct_sql("CAST(s.value AS double precision)")}
FROM processing_runs r
CROSS JOIN LATERAL jsonb_each(
    CASE WHEN jsonb_typeof(r.stage_ms) = 'object' THEN r.stage_ms ELSE jsonb_build_object() END) AS s(key, value)
WHERE r.owner_id = :owner_id AND r.started_at >= :since AND jsonb_typeof(s.value) = 'number'
GROUP BY s.key
ORDER BY s.key
""")

# Final failures only, so the counts add up to `failed`; retried attempts are counted by `retried`.
_RUN_FAILURES = text("""
SELECT coalesce(error_code, 'UNKNOWN') AS key, count(*) AS count
FROM processing_runs
WHERE owner_id = :owner_id AND started_at >= :since AND status = 'failed' AND error_code IS DISTINCT FROM 'SUPERSEDED'
GROUP BY 1
ORDER BY 2 DESC, 1
""")

_RUN_THROUGHPUT = text("""
SELECT h.hour,
       count(r.id) FILTER (WHERE r.status = 'succeeded') AS succeeded,
       count(r.id) FILTER (WHERE r.status = 'failed' AND r.error_code IS DISTINCT FROM 'SUPERSEDED') AS failed
FROM generate_series(date_trunc('hour', CAST(:since AS timestamptz) AT TIME ZONE 'UTC'),
                     date_trunc('hour', CAST(:now AS timestamptz) AT TIME ZONE 'UTC'),
                     interval '1 hour') AS h(hour)
LEFT JOIN processing_runs r
       ON r.owner_id = :owner_id AND r.started_at >= :since
      AND date_trunc('hour', r.started_at AT TIME ZONE 'UTC') = h.hour
GROUP BY h.hour
ORDER BY h.hour
""")

_RUN_RECENT_FAILURES = text("""
SELECT r.document_id, d.filename, r.task, r.error_code, r.error_message,
       coalesce(r.finished_at, r.started_at) AS at
FROM processing_runs r
LEFT JOIN documents d ON d.id = r.document_id AND d.owner_id = r.owner_id
WHERE r.owner_id = :owner_id AND r.started_at >= :since AND r.status = 'failed' AND r.error_code IS DISTINCT FROM 'SUPERSEDED'
ORDER BY at DESC, r.id DESC
LIMIT :limit
""")

# Current state, not windowed. `stale` uses the same rule as the document API's stale flag (services/state.py).
_DOCS_IN_FLIGHT = text("""
SELECT count(*)                                         AS in_flight,
       count(*) FILTER (WHERE updated_at < :stale_before) AS stale
FROM documents
WHERE owner_id = :owner_id AND status IN ('queued', 'processing')
""")


def _llen_queues() -> dict[str, int | None]:
    """LLEN of every Celery queue in one round trip (sync client: call it from a thread)."""
    pipe = get_sync_broker().pipeline(transaction=False)
    for queue in QUEUES:
        pipe.llen(queue)
    return {queue: int(n) for queue, n in zip(QUEUES, pipe.execute(), strict=True)}


async def queue_depth() -> dict[str, int | None]:
    """Messages waiting per queue (global: the queues are shared). Runs in a thread because the redis client is
    sync; if the broker is down or slow the depths are None rather than failing the whole metrics call."""
    with anyio.move_on_after(QUEUE_DEPTH_TIMEOUT_S):
        try:
            return await anyio.to_thread.run_sync(_llen_queues, abandon_on_cancel=True)
        except Exception as exc:  # noqa: BLE001 - Redis down: report unknown depth
            log.warning("queue_depth_unavailable", error=str(exc)[:200])
    return dict.fromkeys(QUEUES)


async def processing_metrics(db: AsyncSession, owner_id: uuid.UUID, window: str) -> ProcessingMetrics:
    now = datetime.now(UTC)
    params = {"owner_id": owner_id, "since": window_start(window, now)}
    counts = (await db.execute(_RUN_COUNTS, params)).mappings().one()
    durations = (await db.execute(_RUN_DURATIONS, params)).mappings()
    stages = (await db.execute(_RUN_STAGES, params)).mappings()
    failures = (await db.execute(_RUN_FAILURES, params)).mappings()
    hours = (await db.execute(_RUN_THROUGHPUT, params | {"now": now})).mappings()
    recent = (await db.execute(_RUN_RECENT_FAILURES, params | {"limit": RECENT_FAILURES})).mappings()
    in_flight = (await db.execute(_DOCS_IN_FLIGHT, {
        "owner_id": owner_id, "stale_before": now - timedelta(minutes=settings.STALE_PROCESSING_MIN),
    })).mappings().one()

    return ProcessingMetrics(
        window=window,
        attempts=counts["attempts"],
        succeeded=counts["succeeded"],
        failed=counts["failed"],
        retried=counts["retried"],
        crashed=counts["crashed"],
        success_rate=_ratio(counts["succeeded"], counts["succeeded"] + counts["failed"]),
        duration_ms={r["key"]: _percentiles(r) for r in durations},
        stage_ms={r["key"]: _percentiles(r) for r in stages},
        failures_by_code=[CountByKey(key=r["key"], count=r["count"]) for r in failures],
        queue_depth=await queue_depth(),
        in_flight=in_flight["in_flight"],
        stale=in_flight["stale"],
        throughput=[HourBucket(hour=r["hour"].strftime("%Y-%m-%dT%H:00:00Z"), succeeded=r["succeeded"],
                               failed=r["failed"]) for r in hours],
        recent_failures=[
            RecentFailure(
                document_id=str(r["document_id"]) if r["document_id"] else None, filename=r["filename"],
                task=r["task"], error_code=r["error_code"], error_message=(r["error_message"] or "")[:300] or None,
                at=r["at"].isoformat(),
            )
            for r in recent
        ],
    )


# --- GET /metrics/ai-usage ---------------------------------------------------------------------------------------
_USAGE_TOTALS = text("""
SELECT count(*)                                         AS calls,
       coalesce(sum(input_tokens), 0)                   AS input_tokens,
       coalesce(sum(cached_input_tokens), 0)            AS cached_input_tokens,
       coalesce(sum(output_tokens), 0)                  AS output_tokens,
       coalesce(sum(reasoning_tokens), 0)               AS reasoning_tokens,
       coalesce(sum(cost_usd), 0)                       AS cost_usd,
       avg(CASE WHEN cache_hit THEN 1.0 ELSE 0.0 END)   AS cache_hit_rate,
       avg(CASE WHEN estimated THEN 1.0 ELSE 0.0 END)   AS estimated_share,
       avg(CASE WHEN success THEN 0.0 ELSE 1.0 END)     AS error_rate
FROM ai_usage
WHERE owner_id = :owner_id AND created_at >= :since
""")

# The only SQL that varies by request: group_by picks one of these fixed expressions (never user text).
_USAGE_GROUP_KEYS: dict[str, str] = {
    "operation": "operation",
    "model": "model",
    "day": "to_char(created_at AT TIME ZONE 'UTC', 'YYYY-MM-DD')",
}


def _usage_groups_sql(group_by: str) -> TextClause:
    order = "key" if group_by == "day" else "cost_usd DESC, calls DESC, key"
    return text(f"""
SELECT {_USAGE_GROUP_KEYS[group_by]} AS key,
       count(*)                        AS calls,
       coalesce(sum(input_tokens), 0)  AS input_tokens,
       coalesce(sum(output_tokens), 0) AS output_tokens,
       coalesce(sum(cost_usd), 0)      AS cost_usd,
       avg(latency_ms)                 AS avg_latency_ms
FROM ai_usage
WHERE owner_id = :owner_id AND created_at >= :since
GROUP BY 1
ORDER BY {order}
""")


# Unit economics. A "document" is one that incurred processing cost in the window (embed_chunks / insights rows
# carry document_id). An "answer" is an assistant message, including gate refusals that only paid for the query
# embedding, so this is the true average cost of a chat turn.
_USAGE_UNIT_COSTS = text("""
WITH spend AS (
  SELECT coalesce(sum(cost_usd) FILTER (WHERE operation IN ('embed_chunks', 'insights')), 0) AS document_cost,
         count(DISTINCT document_id) FILTER (WHERE operation IN ('embed_chunks', 'insights'))   AS documents,
         coalesce(sum(cost_usd) FILTER (
             WHERE operation IN ('condense', 'embed_query', 'answer', 'followups')), 0)        AS answer_cost
  FROM ai_usage
  WHERE owner_id = :owner_id AND created_at >= :since
)
SELECT spend.*,
       (SELECT count(*) FROM chat_messages m JOIN chat_sessions s ON s.id = m.session_id
         WHERE s.owner_id = :owner_id AND m.role = 'assistant' AND m.created_at >= :since) AS answers
FROM spend
""")


def _per_unit(cost: Decimal, units: int) -> float | None:
    return round(float(cost) / units, 6) if units else None


async def ai_usage_metrics(
    db: AsyncSession, owner_id: uuid.UUID, window: str, group_by: str, daily_limit_usd: Decimal,
) -> AIUsageMetrics:
    params = {"owner_id": owner_id, "since": window_start(window)}
    t = (await db.execute(_USAGE_TOTALS, params)).mappings().one()
    groups = (await db.execute(_usage_groups_sql(group_by), params)).mappings()
    unit = (await db.execute(_USAGE_UNIT_COSTS, params)).mappings().one()

    return AIUsageMetrics(
        window=window,
        group_by=group_by,
        totals=UsageTotals(
            calls=t["calls"], input_tokens=t["input_tokens"], cached_input_tokens=t["cached_input_tokens"],
            output_tokens=t["output_tokens"], reasoning_tokens=t["reasoning_tokens"],
            cost_usd=round(float(t["cost_usd"]), 6), cache_hit_rate=_num(t["cache_hit_rate"], 4),
            estimated_share=_num(t["estimated_share"], 4), error_rate=_num(t["error_rate"], 4),
        ),
        groups=[
            UsageGroup(key=r["key"], calls=r["calls"], input_tokens=r["input_tokens"],
                       output_tokens=r["output_tokens"], cost_usd=round(float(r["cost_usd"]), 6),
                       avg_latency_ms=_num(r["avg_latency_ms"]))
            for r in groups
        ],
        cost_per_document_usd=_per_unit(unit["document_cost"], unit["documents"]),
        cost_per_answer_usd=_per_unit(unit["answer_cost"], unit["answers"]),
        today_spend_usd=round(float(await today_spend_usd(db, owner_id)), 6),
        daily_limit_usd=float(daily_limit_usd),
    )


# --- GET /metrics/chat -------------------------------------------------------------------------------------------
# Assistant rows of the caller's sessions. Contract with the chat package: timings {condense, embed, retrieve, ttft,
# total} in ms; signals {gate, invalid_citations, uncited_claims, ...}; warnings may contain "fallback_model".
_ANSWERS_CTE = """
WITH answers AS (
  SELECT m.*
  FROM chat_messages m JOIN chat_sessions s ON s.id = m.session_id
  WHERE s.owner_id = :owner_id AND m.role = 'assistant' AND m.created_at >= :since
)
"""
_GATED = "coalesce(CAST(signals ->> 'gate' AS boolean), false)"   # refused by the retrieval gate, no LLM call

_CHAT_SUMMARY = text(_ANSWERS_CTE + f"""
SELECT count(*) AS answers,
       {_pct_sql("coalesce(ttft_ms, CAST(timings ->> 'ttft' AS double precision))", "ttft_")},
       {_pct_sql("coalesce(latency_ms, CAST(timings ->> 'total' AS double precision))", "latency_")},
       avg(CASE WHEN jsonb_typeof(citations) = 'array' THEN jsonb_array_length(citations) END)
           FILTER (WHERE answer_type = 'answered')                              AS avg_citations,
       coalesce(sum(CAST(signals ->> 'invalid_citations' AS numeric)), 0)      AS invalid_citations,
       coalesce(sum(CAST(signals ->> 'uncited_claims' AS numeric)), 0)         AS uncited_claims,
       count(*) FILTER (WHERE warnings @> '["fallback_model"]')                AS fallback_model,
       count(*) FILTER (WHERE status = 'failed')                               AS errors,
       count(*) FILTER (WHERE status = 'interrupted')                          AS interrupted,
       count(*) FILTER (WHERE answer_type = 'not_found' AND {_GATED})         AS not_found_gate,
       count(*) FILTER (WHERE answer_type = 'not_found' AND NOT {_GATED})     AS not_found_model
FROM answers
""")

_CHAT_BREAKDOWNS = text(_ANSWERS_CTE + """
SELECT 'answer_type' AS dim, answer_type AS key, count(*) AS n FROM answers
WHERE answer_type IS NOT NULL GROUP BY answer_type
UNION ALL
SELECT 'confidence', confidence, count(*) FROM answers
WHERE confidence IS NOT NULL GROUP BY confidence
""")

# Every numeric timing except ttft/total (reported as ttft_ms / latency_ms), so new stages show up automatically.
_CHAT_STAGES = text(_ANSWERS_CTE + f"""
SELECT t.key, {_pct_sql("CAST(t.value AS double precision)")}
FROM answers a
CROSS JOIN LATERAL jsonb_each(
    CASE WHEN jsonb_typeof(a.timings) = 'object' THEN a.timings ELSE jsonb_build_object() END) AS t(key, value)
WHERE jsonb_typeof(t.value) = 'number' AND t.key NOT IN ('ttft', 'total')
GROUP BY t.key
ORDER BY t.key
""")


async def chat_metrics(db: AsyncSession, owner_id: uuid.UUID, window: str) -> ChatMetrics:
    params = {"owner_id": owner_id, "since": window_start(window)}
    s = (await db.execute(_CHAT_SUMMARY, params)).mappings().one()
    breakdowns: dict[str, dict[str, int]] = {
        "answer_type": dict.fromkeys(ANSWER_TYPES, 0), "confidence": dict.fromkeys(CONFIDENCE_LEVELS, 0),
    }
    for row in (await db.execute(_CHAT_BREAKDOWNS, params)).mappings():
        breakdowns[row["dim"]][row["key"]] = row["n"]
    stages = (await db.execute(_CHAT_STAGES, params)).mappings()

    return ChatMetrics(
        window=window,
        answers=s["answers"],
        ttft_ms=_percentiles(s, "ttft_"),
        latency_ms=_percentiles(s, "latency_"),
        stage_ms={r["key"]: _percentiles(r) for r in stages},
        answer_types=breakdowns["answer_type"],
        not_found_by_path={"gate": s["not_found_gate"], "model": s["not_found_model"]},
        confidence=breakdowns["confidence"],
        avg_citations=_num(s["avg_citations"], 2),
        invalid_citations=int(s["invalid_citations"]),
        uncited_claims=int(s["uncited_claims"]),
        fallback_model=s["fallback_model"],
        errors=s["errors"],
        interrupted=s["interrupted"],
    )


# --- GET /metrics/system (global) --------------------------------------------------------------------------------
_SYSTEM_REQUESTS = text("SELECT count(*) FROM request_logs WHERE created_at >= :since")

# Percentiles use ttfb_ms: for a streamed answer total_ms is the whole stream and would hide real latency.
_SYSTEM_ROUTES = text(f"""
SELECT method, route, count(*) AS count, {_pct_sql("ttfb_ms")},
       avg(CASE WHEN status_code BETWEEN 400 AND 499 THEN 1.0 ELSE 0.0 END) AS error_4xx_rate,
       avg(CASE WHEN status_code >= 500 THEN 1.0 ELSE 0.0 END)            AS error_5xx_rate
FROM request_logs
WHERE created_at >= :since
GROUP BY method, route
ORDER BY count(*) DESC, route, method
LIMIT :limit
""")


async def system_metrics(db: AsyncSession, window: str) -> SystemMetrics:
    params = {"since": window_start(window)}
    requests = await db.scalar(_SYSTEM_REQUESTS, params)
    routes = (await db.execute(_SYSTEM_ROUTES, params | {"limit": MAX_ROUTES})).mappings()
    return SystemMetrics(
        window=window,
        requests=requests or 0,
        routes=[
            RouteStats(method=r["method"], route=r["route"], count=r["count"], ttfb_ms=_percentiles(r),
                       error_4xx_rate=round(float(r["error_4xx_rate"]), 4),
                       error_5xx_rate=round(float(r["error_5xx_rate"]), 4))
            for r in routes
        ],
    )
