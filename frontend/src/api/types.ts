/**
 * TypeScript mirrors of the frozen API contracts in app/schemas/*.py. Field names match the Pydantic models exactly.
 *
 * Wire-format notes:
 * - UUIDs, datetimes and dates arrive as strings.
 * - Pydantic serialises `Decimal` as a JSON *string* ("0.001234"), so Decimal fields are typed `Money` and must go
 *   through `Number()` before maths or formatting (see lib/format.ts).
 * - The server may add fields at any time (clients ignore unknown fields), so optional extras are typed as optional.
 */

export type Uuid = string
export type IsoDateTime = string
export type Money = string | number

// ---- common.py -------------------------------------------------------------------------------------------------
export interface Page<T> {
  items: T[]
  next_cursor: string | null
}

export interface ErrorBody {
  code: string
  message: string
  details: Record<string, unknown>
  request_id: string | null
}

export interface ErrorEnvelope {
  error: ErrorBody
}

// ---- documents.py ----------------------------------------------------------------------------------------------
export type DocumentStatus = 'queued' | 'processing' | 'ready' | 'failed'
export type InsightsStatus = 'pending' | 'processing' | 'ready' | 'failed'

export interface DocumentError {
  code: string
  message: string
  retryable: boolean
}

export interface DocumentOut {
  id: Uuid
  filename: string
  title: string | null
  mime_type: string
  size_bytes: number
  sha256: string
  status: DocumentStatus
  stage: string | null
  progress_pct: number
  stale: boolean
  insights_status: InsightsStatus
  insights_error_code: string | null
  page_count: number | null
  chunk_count: number | null
  token_count: number | null
  category: string | null
  tags: string[]
  document_date: string | null
  version: number
  upload_count: number
  warnings: unknown[]
  error: DocumentError | null
  created_at: IsoDateTime
  updated_at: IsoDateTime
  ready_at: IsoDateTime | null
}

export interface UploadLinks {
  self: string
  events: string
  insights: string
}

export interface UploadResponse {
  document: DocumentOut
  duplicate: boolean
  requeued: boolean
  links: UploadLinks
}

/** Payload of `event: status` on GET /documents/{id}/events. */
export interface DocumentStatusEvent {
  status: DocumentStatus
  stage: string | null
  progress_pct: number
  insights_status: InsightsStatus
  error: DocumentError | null
  stale: boolean
}

export interface PageOut {
  document_id: Uuid
  page_no: number
  label: string
  heading_path: string | null
  text: string
}

export type ReprocessScope = 'all' | 'insights'

export interface ReprocessResponse {
  document: DocumentOut
  run_id: Uuid
}

export type SummaryLength = 'short' | 'medium' | 'detailed'
export type SummaryTone = 'neutral' | 'executive' | 'technical' | 'simple'

export interface SummaryRequest {
  length: SummaryLength
  tone: SummaryTone
  focus: string | null
}

export interface SummaryOut {
  document_id: Uuid
  length: string
  tone: string
  focus: string | null
  content: string
  cached: boolean
  model: string
  prompt_version: string
}

// ---- insights.py -----------------------------------------------------------------------------------------------
export const CATEGORIES = [
  'contract',
  'policy',
  'financial_report',
  'proposal',
  'meeting_notes',
  'technical',
  'other',
] as const
export type Category = (typeof CATEGORIES)[number]

export interface KeyPoint {
  point: string
  page: number | null
}

export interface Entity {
  name: string
  type: 'person' | 'organization' | 'date' | 'amount' | 'location' | 'other'
}

export interface RiskFlag {
  type: string
  severity: 'low' | 'medium' | 'high'
  quote: string
  page: number | null
}

export interface DocumentInsights {
  title: string
  summary: string
  key_points: KeyPoint[]
  document_type: string
  category: Category
  tags: string[]
  entities: Entity[]
  sentiment: 'positive' | 'neutral' | 'negative' | 'mixed'
  sentiment_rationale: string
  risk_flags: RiskFlag[]
  suggested_questions: string[]
  document_date: string | null
  language: string
  injection_suspected: boolean
  /** Added by the server after verification (InsightsOut.insights is "DocumentInsights + coverage/model/prompt_version"). */
  coverage?: 'full' | 'sampled'
  model?: string | null
  prompt_version?: string | null
}

export interface InsightsOut {
  document_id: Uuid
  status: InsightsStatus
  error_code: string | null
  insights: DocumentInsights | null
  updated_at: IsoDateTime | null
}

// ---- chat.py ---------------------------------------------------------------------------------------------------
export interface SessionCreate {
  document_ids: Uuid[]
  all_documents: boolean
  title: string | null
}

export interface SessionOut {
  id: Uuid
  title: string | null
  all_documents: boolean
  document_ids: Uuid[]
  created_at: IsoDateTime
  last_message_at: IsoDateTime | null
  starter_questions: string[]
  warnings: string[]
}

export interface MessageCreate {
  content: string
  client_message_id: string | null
}

export interface Citation {
  ref: string
  chunk_id: number
  document_id: Uuid
  filename: string
  label: string
  page_start: number
  page_end: number
  snippet: string | null
  weak_support: boolean
  source_deleted: boolean
}

export interface SourceRef {
  ref: string
  chunk_id: number
  document_id: Uuid
  filename: string
  label: string
}

export interface Signals {
  top_similarity: number | null
  id_hit: boolean
  lexical_match: boolean
  valid_citations: number
  invalid_citations: number
  uncited_claims: number
  ungrounded_facts: number
  gate: boolean
}

export interface UsageOut {
  input_tokens: number
  cached_input_tokens: number
  output_tokens: number
  cost_usd: Money
  estimated: boolean
}

/** One retrieval candidate stored in chat_messages.retrieval (design §3: {chunk_id, ref, rrf, sem_rank, lex_rank,
 *  sem_dist, id_hit}). `ref` is set only for candidates that made the top-k sent to the model. */
export interface RetrievalCandidate {
  chunk_id: number
  ref?: string | null
  rrf?: number | null
  sem_rank?: number | null
  lex_rank?: number | null
  sem_dist?: number | null
  similarity?: number | null
  id_hit?: boolean
  document_id?: Uuid
  filename?: string
  label?: string
}

/** chat_messages.retrieval JSON: {mode, gate, top_similarity, terms, id_terms, candidates[]}. Free-form on the wire
 *  (MessageOut.retrieval is dict[str, Any]), so every field is optional here. */
export interface RetrievalInfo {
  mode?: string
  gate?: boolean
  top_similarity?: number | null
  terms?: string[]
  id_terms?: string[]
  candidates?: RetrievalCandidate[]
}

export type AnswerType = 'answered' | 'not_found' | 'ungrounded'
export type Confidence = 'high' | 'medium' | 'low'
export type MessageStatus = 'streaming' | 'complete' | 'interrupted' | 'failed'

export interface MessageOut {
  id: Uuid
  seq: number
  session_id: Uuid
  role: 'user' | 'assistant'
  content: string
  status: MessageStatus
  reply_to_id: Uuid | null
  standalone_query: string | null
  answer_type: AnswerType | null
  confidence: Confidence | null
  signals: Partial<Signals> | null
  retrieval: RetrievalInfo | null
  citations: Citation[]
  followups: string[]
  warnings: unknown[]
  model: string | null
  prompt_version: string | null
  input_tokens: number | null
  output_tokens: number | null
  cost_usd: Money | null
  ttft_ms: number | null
  latency_ms: number | null
  created_at: IsoDateTime
  /** Not in MessageOut today; shown when present (the column exists: chat_messages.timings). */
  timings?: Record<string, number> | null
}

export interface AskResponse {
  user_message: MessageOut
  answer: MessageOut
}

// SSE payloads of POST /chat/sessions/{id}/messages/stream
export interface MetaEvent {
  message_id: Uuid
  user_message_id: Uuid
  standalone_query: string | null
  sources: SourceRef[]
  retrieval_mode: string
  warnings: string[]
}

export interface TokenEvent {
  delta: string
}

export interface CitationsEvent {
  citations: Citation[]
  answer_type: AnswerType
  confidence: Confidence
  signals: Signals
}

export interface FollowupsEvent {
  questions: string[]
}

export interface DoneEvent {
  message_id: Uuid
  usage: UsageOut
  model: string | null
  ttft_ms: number | null
  latency_ms: number
  timings: Record<string, number>
}

export interface ErrorEvent {
  code: string
  message: string
}

// ---- search.py -------------------------------------------------------------------------------------------------
export interface SearchHit {
  chunk_id: number
  document_id: Uuid
  filename: string
  label: string
  page_start: number
  page_end: number
  rrf: number
  sem_rank: number | null
  lex_rank: number | null
  similarity: number | null
  id_hit: boolean
  snippet: string
  content: string
}

export interface SearchResponse {
  query: string
  mode: string
  terms: string[]
  id_terms: string[]
  top_similarity: number | null
  hits: SearchHit[]
}

// ---- metrics.py ------------------------------------------------------------------------------------------------
export interface Percentiles {
  p50: number | null
  p95: number | null
  avg: number | null
}

export interface CountByKey {
  key: string
  count: number
}

export interface DayCount {
  day: string
  count: number
}

export interface DocumentTotals {
  documents: number
  pages: number
  chunks: number
  bytes: number
  tokens: number
  dedup_hits: number
}

export interface DocumentStats {
  totals: DocumentTotals
  by_status: Record<string, number>
  by_insights_status: Record<string, number>
  by_mime_type: Record<string, number>
  by_category: Record<string, number>
  top_tags: CountByKey[]
  uploads_per_day: DayCount[]
  seconds_to_ready: Percentiles
}

export interface HourBucket {
  hour: string
  succeeded: number
  failed: number
}

export interface RecentFailure {
  document_id: string | null
  filename: string | null
  task: string
  error_code: string | null
  error_message: string | null
  at: string
}

export interface ProcessingMetrics {
  window: string
  attempts: number
  succeeded: number
  failed: number
  retried: number
  crashed: number
  success_rate: number | null
  duration_ms: Record<string, Percentiles>
  stage_ms: Record<string, Percentiles>
  failures_by_code: CountByKey[]
  queue_depth: Record<string, number | null>
  in_flight: number
  stale: number
  throughput: HourBucket[]
  recent_failures: RecentFailure[]
}

export interface UsageTotals {
  calls: number
  input_tokens: number
  cached_input_tokens: number
  output_tokens: number
  reasoning_tokens: number
  cost_usd: number
  cache_hit_rate: number | null
  estimated_share: number | null
  error_rate: number | null
}

export interface UsageGroup {
  key: string
  calls: number
  input_tokens: number
  output_tokens: number
  cost_usd: number
  avg_latency_ms: number | null
}

export type UsageGroupBy = 'operation' | 'model' | 'day'

export interface AIUsageMetrics {
  window: string
  group_by: UsageGroupBy
  totals: UsageTotals
  groups: UsageGroup[]
  cost_per_document_usd: number | null
  cost_per_answer_usd: number | null
  today_spend_usd: number
  daily_limit_usd: number
}

export interface ChatMetrics {
  window: string
  answers: number
  ttft_ms: Percentiles
  latency_ms: Percentiles
  stage_ms: Record<string, Percentiles>
  answer_types: Record<string, number>
  not_found_by_path: Record<string, number>
  confidence: Record<string, number>
  avg_citations: number | null
  invalid_citations: number
  uncited_claims: number
  fallback_model: number
  errors: number
  interrupted: number
}

export interface RouteStats {
  method: string
  route: string
  count: number
  ttfb_ms: Percentiles
  error_4xx_rate: number
  error_5xx_rate: number
}

export interface SystemMetrics {
  window: string
  requests: number
  routes: RouteStats[]
}

// ---- health (app/api/health.py; not a Pydantic model) ----------------------------------------------------------
export type HealthStatus = 'ok' | 'degraded' | 'unavailable'

export interface HealthCheck {
  ok: boolean
  ms?: number
  error?: string
  [extra: string]: unknown
}

export interface ReadyResponse {
  status: HealthStatus
  checks: Record<string, HealthCheck>
  models?: Record<string, string>
}
