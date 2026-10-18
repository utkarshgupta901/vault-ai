import { useEffect, useRef, useState } from 'react'
import { ApiError } from '../api/client'
import { followDocumentEvents } from '../api/documents'
import type { DocumentOut, DocumentStatusEvent } from '../api/types'
import { ReconnectBackoff } from '../lib/reconnect'
import { useLatest } from './useApi'

/**
 * Every SSE stream holds one HTTP connection open, and browsers allow only ~6 concurrent HTTP/1.1 connections per
 * origin. Following every in-flight upload at once could starve normal requests (list, health, chat), so at most
 * MAX_STREAMS documents are followed at a time; the others wait for a slot. Slots free up quickly because the server
 * ends each stream as soon as its document reaches a terminal state.
 */
const MAX_STREAMS = 3

/** Still changing: ingest running, or ready but insights not finished yet. */
export function needsLiveStatus(doc: Pick<DocumentOut, 'status' | 'insights_status'>): boolean {
  if (doc.status === 'queued' || doc.status === 'processing') return true
  return doc.status === 'ready' && (doc.insights_status === 'pending' || doc.insights_status === 'processing')
}

/** Mirrors the server's own end-of-stream rule in app/api/documents.py (_is_terminal). */
export function isTerminal(ev: DocumentStatusEvent): boolean {
  return ev.status === 'failed' || (ev.status === 'ready' && (ev.insights_status === 'ready' || ev.insights_status === 'failed'))
}

/** Applies a status event to a document row (the event carries only the fields that change while processing). */
export function applyStatusEvent(doc: DocumentOut, ev: DocumentStatusEvent): DocumentOut {
  return {
    ...doc,
    status: ev.status,
    stage: ev.stage,
    progress_pct: ev.progress_pct,
    insights_status: ev.insights_status,
    error: ev.error,
    stale: ev.stale,
  }
}

/** Follows GET /documents/{id}/events for each in-flight document in `docs` and reports changes to the caller. */
export function useDocumentEvents(
  docs: DocumentOut[],
  onStatus: (id: string, ev: DocumentStatusEvent) => void,
  onDeleted: (id: string) => void,
): void {
  const handlers = useLatest({ onStatus, onDeleted })
  const streams = useRef(new Map<string, AbortController>())
  // Back-off before re-following a document whose stream ended early (network blip, the server's 10-minute cap).
  const reconnects = useRef(new ReconnectBackoff())
  const [slotFreed, setSlotFreed] = useState(0)
  // A string key so the effect re-runs only when the *set* of in-flight ids changes, not on every row update.
  const wanted = docs
    .filter(needsLiveStatus)
    .map((d) => d.id)
    .join(',')

  useEffect(() => {
    const ids = wanted ? wanted.split(',') : []
    const active = streams.current
    const backoff = reconnects.current
    for (const [id, controller] of active) {
      if (!ids.includes(id)) {
        controller.abort()
        active.delete(id)
      }
    }
    for (const id of ids) {
      if (active.size >= MAX_STREAMS) break
      if (active.has(id) || backoff.isStopped(id)) continue
      const controller = new AbortController()
      active.set(id, controller)
      let sawTerminal = false
      let failedStatus: number | null = null
      followDocumentEvents(
        id,
        {
          onStatus: (ev) => {
            sawTerminal = isTerminal(ev)
            backoff.connected(id)
            handlers.current.onStatus(id, ev)
          },
          onDeleted: () => {
            sawTerminal = true
            handlers.current.onDeleted(id)
          },
        },
        controller.signal,
      )
        .catch((err: unknown) => {
          // network/auth failure: handled like an early end below; the status says whether retrying can help
          if (err instanceof ApiError) failedStatus = err.status
        })
        .finally(() => {
          if (controller.signal.aborted) return // we stopped it on purpose
          const release = () => {
            if (active.get(id) !== controller) return
            active.delete(id)
            setSlotFreed((n) => n + 1) // re-run the effect: start a waiting document, or reconnect this one
          }
          if (sawTerminal) return release()
          const wait = backoff.ended(id, failedStatus)
          if (wait === null) release() // 401/403/404: free the slot; the loop above skips this document from now on
          else setTimeout(release, wait)
        })
    }
  }, [wanted, slotFreed, handlers])

  useEffect(() => {
    const active = streams.current
    return () => {
      for (const controller of active.values()) controller.abort()
      active.clear()
    }
  }, [])
}
