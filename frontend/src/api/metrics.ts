/** Typed calls for /api/v1/metrics/* (app/schemas/metrics.py) and the readiness probe. */
import { getApiKey, request } from './client'
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

/** /health/ready answers 503 *with a JSON body* when a hard dependency is down, so read the body for any status
 *  instead of treating 503 as an exception. Returns null when the API is unreachable. */
export async function getReadiness(signal?: AbortSignal): Promise<ReadyResponse | null> {
  try {
    const res = await fetch('/health/ready', { headers: { 'X-API-Key': getApiKey() }, signal })
    return (await res.json()) as ReadyResponse
  } catch {
    return null
  }
}
