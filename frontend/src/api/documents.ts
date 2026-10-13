/** Typed calls for /api/v1/documents/* (app/api/documents.py) and the insights/summaries endpoints. */
import { ApiError, request, send } from './client'
import { openEventStream, parseData } from './sse'
import type {
  DocumentOut,
  DocumentStatus,
  DocumentStatusEvent,
  InsightsOut,
  Page,
  PageOut,
  ReprocessResponse,
  ReprocessScope,
  SummaryOut,
  SummaryRequest,
  UploadResponse,
} from './types'

const BASE = '/api/v1/documents'

export interface DocumentFilters {
  status?: DocumentStatus | ''
  category?: string
  tag?: string
  q?: string
}

export function listDocuments(filters: DocumentFilters, cursor: string | null, signal?: AbortSignal, limit = 20) {
  return request<Page<DocumentOut>>(BASE, { query: { ...filters, cursor, limit }, signal })
}

export function getDocument(id: string, signal?: AbortSignal) {
  return request<DocumentOut>(`${BASE}/${id}`, { signal })
}

/** One multipart request per file (field name `file`). 202 = queued, 200 = duplicate (no new work, no new cost). */
export function uploadDocument(file: File) {
  const form = new FormData()
  form.append('file', file)
  return request<UploadResponse>(BASE, { method: 'POST', body: form })
}

export function deleteDocument(id: string) {
  return request<void>(`${BASE}/${id}`, { method: 'DELETE' })
}

export function reprocessDocument(id: string, scope: ReprocessScope = 'all') {
  return request<ReprocessResponse>(`${BASE}/${id}/reprocess`, { method: 'POST', json: { scope } })
}

export function getPage(id: string, pageNo: number, signal?: AbortSignal) {
  return request<PageOut>(`${BASE}/${id}/pages/${pageNo}`, { signal })
}

export function getInsights(id: string, signal?: AbortSignal) {
  return request<InsightsOut>(`${BASE}/${id}/insights`, { signal })
}

export function createSummary(id: string, body: SummaryRequest) {
  return request<SummaryOut>(`${BASE}/${id}/summaries`, { method: 'POST', json: body })
}

/** The file route needs the X-API-Key header, so a plain <a href> cannot download it: fetch it as a blob and hand
 *  the browser an object URL instead. */
export async function downloadOriginal(id: string, filename: string): Promise<void> {
  const res = await send(`${BASE}/${id}/file`, { accept: '*/*' })
  const url = URL.createObjectURL(await res.blob())
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.append(a)
  a.click()
  a.remove()
  // Give the browser a moment to start the download before the URL is revoked.
  setTimeout(() => URL.revokeObjectURL(url), 10_000)
}

export interface DocumentEventHandlers {
  onStatus: (ev: DocumentStatusEvent) => void
  onDeleted: () => void
}

/** Follows GET /documents/{id}/events until the server ends it (terminal state or 10 min) or `signal` aborts.
 *  Unknown event names are ignored, per the SSE contract. */
export async function followDocumentEvents(id: string, handlers: DocumentEventHandlers, signal: AbortSignal) {
  try {
    await openEventStream(`${BASE}/${id}/events`, { signal }, (msg) => {
      if (msg.event === 'status') handlers.onStatus(parseData<DocumentStatusEvent>(msg))
      else if (msg.event === 'deleted') handlers.onDeleted()
    })
  } catch (err) {
    if (err instanceof ApiError && err.code === 'DOCUMENT_NOT_FOUND') handlers.onDeleted()
    else throw err
  }
}
