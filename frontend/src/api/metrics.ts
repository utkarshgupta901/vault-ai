/** Typed calls for /api/v1/metrics/* (app/schemas/metrics.py) and the readiness probe. */
import { getApiKey, request } from './client'
import { fetchReadiness } from './readiness'
import type {
  AIUsageMetrics,
  ChatMetrics,
  DocumentStats,
  ProcessingMetrics,
  ReadyResponse,
  SystemMetrics,
  UsageGroupBy,
} from './types'

const BASE = '/api/v1/metrics'

export const getDocumentMetrics = (signal?: AbortSignal) => request<DocumentStats>(`${BASE}/documents`, { signal })

export const getProcessingMetrics = (signal?: AbortSignal) =>
  request<ProcessingMetrics>(`${BASE}/processing`, { query: { window: '24h' }, signal })

export const getAiUsageMetrics = (groupBy: UsageGroupBy, signal?: AbortSignal) =>
  request<AIUsageMetrics>(`${BASE}/ai-usage`, { query: { window: '7d', group_by: groupBy }, signal })

export const getChatMetrics = (signal?: AbortSignal) =>
  request<ChatMetrics>(`${BASE}/chat`, { query: { window: '7d' }, signal })

export const getSystemMetrics = (signal?: AbortSignal) =>
  request<SystemMetrics>(`${BASE}/system`, { query: { window: '1h' }, signal })

/** Readiness probe for the health dot: null when the API is unreachable or slower than READY_TIMEOUT_MS
 *  (see readiness.ts for the timeout and abort handling). */
export const getReadiness = (signal?: AbortSignal): Promise<ReadyResponse | null> =>
  fetchReadiness({ 'X-API-Key': getApiKey() }, signal)
