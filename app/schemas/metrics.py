"""Response contracts for the metrics APIs (frozen in WP0: the metrics service and the dashboard both build on them).

Data sources: documents, processing_runs (one row per attempt), ai_usage (one row per model call),
chat_messages (assistant rows: timings/signals/warnings/answer_type/confidence), request_logs."""

from typing import Literal

from pydantic import BaseModel


class Percentiles(BaseModel):
    p50: float | None = None
    p95: float | None = None
    avg: float | None = None


class CountByKey(BaseModel):
    key: str
    count: int


class DayCount(BaseModel):
    day: str            # YYYY-MM-DD
    count: int


# --- GET /metrics/documents ----------------------------------------------------------------------------
class DocumentTotals(BaseModel):
    documents: int = 0
    pages: int = 0
    chunks: int = 0
    bytes: int = 0
    tokens: int = 0
    dedup_hits: int = 0          # sum(upload_count - 1): uploads that cost nothing


class DocumentStats(BaseModel):
    totals: DocumentTotals
    by_status: dict[str, int]
    by_insights_status: dict[str, int]
    by_mime_type: dict[str, int]
    by_category: dict[str, int]
    top_tags: list[CountByKey]
    uploads_per_day: list[DayCount]          # last 14 days
    seconds_to_ready: Percentiles            # created_at -> ready_at


# --- GET /metrics/processing?window=24h ----------------------------------------------------------------------
class HourBucket(BaseModel):
    hour: str           # ISO hour
    succeeded: int
    failed: int


class RecentFailure(BaseModel):
    document_id: str | None
    filename: str | None
    task: str
    error_code: str | None
    error_message: str | None
    at: str


class ProcessingMetrics(BaseModel):
    window: str
    attempts: int
    succeeded: int
    failed: int
    retried: int                             # attempts with status 'retrying'
    crashed: int                             # error_code WORKER_LOST
    success_rate: float | None               # succeeded / (succeeded + failed)
    duration_ms: dict[str, Percentiles]      # by task: ingest | insights
    stage_ms: dict[str, Percentiles]         # by stage key in processing_runs.stage_ms (download/extract/chunk/embed/llm/...)
    failures_by_code: list[CountByKey]
    queue_depth: dict[str, int | None]       # Redis LLEN per queue (None if Redis unreachable)
    in_flight: int                           # documents queued/processing
    stale: int                               # queued/processing with no update for STALE_PROCESSING_MIN
    throughput: list[HourBucket]
    recent_failures: list[RecentFailure]


# --- GET /metrics/ai-usage?window=7d&group_by=operation|model|day ----------------------------------------------
class UsageTotals(BaseModel):
    calls: int = 0
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    cost_usd: float = 0.0
    cache_hit_rate: float | None = None
    estimated_share: float | None = None     # share of calls whose usage was estimated (interrupted streams)
    error_rate: float | None = None


class UsageGroup(BaseModel):
    key: str
    calls: int
    input_tokens: int
    output_tokens: int
    cost_usd: float
    avg_latency_ms: float | None


class AIUsageMetrics(BaseModel):
    window: str
    group_by: Literal["operation", "model", "day"]
    totals: UsageTotals
    groups: list[UsageGroup]
    cost_per_document_usd: float | None      # (embed_chunks + insights) cost / documents processed in window
    cost_per_answer_usd: float | None        # (condense + embed_query + answer + followups) / answers in window
    today_spend_usd: float
    daily_limit_usd: float


# --- GET /metrics/chat?window=7d -------------------------------------------------------------------------------
class ChatMetrics(BaseModel):
    window: str
    answers: int
    ttft_ms: Percentiles
    latency_ms: Percentiles
    stage_ms: dict[str, Percentiles]         # condense / embed / retrieve from chat_messages.timings
    answer_types: dict[str, int]             # answered / not_found / ungrounded
    not_found_by_path: dict[str, int]        # gate (no LLM call) / model (LLM refused)
    confidence: dict[str, int]               # high / medium / low
    avg_citations: float | None
    invalid_citations: int
    uncited_claims: int
    fallback_model: int                      # answers served by the fallback model
    errors: int                              # status failed
    interrupted: int


# --- GET /metrics/system?window=1h ---------------------------------------------------------------------------
class RouteStats(BaseModel):
    method: str
    route: str
    count: int
    ttfb_ms: Percentiles
    error_4xx_rate: float
    error_5xx_rate: float


class SystemMetrics(BaseModel):
    window: str
    requests: int
    routes: list[RouteStats]
