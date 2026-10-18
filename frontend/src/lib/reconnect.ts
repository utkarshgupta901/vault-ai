/**
 * When useDocumentEvents re-follows a document whose event stream ended early, kept free of React so `pnpm test` can
 * check the schedule under node.
 *
 * It used to reconnect every 3 s forever, even when retrying could not help: with a revoked API key (401) or a route
 * this server does not have (404) every followed document sent a doomed request every 3 s for as long as the page
 * stayed open. Now the wait doubles per consecutive early end, from 3 s up to 60 s, and those statuses stop following
 * the document for the life of the page (saving a new API key reloads the page anyway).
 */

export const RECONNECT_BASE_MS = 3_000
export const RECONNECT_MAX_MS = 60_000

/** Wait after `failures` consecutive early ends (1 = the first): 3 s, 6 s, 12 s, 24 s, 48 s, then 60 s. */
export function reconnectDelay(failures: number): number {
  return Math.min(RECONNECT_MAX_MS, RECONNECT_BASE_MS * 2 ** Math.max(0, failures - 1))
}

/** Statuses a retry cannot fix. A deleted document never gets here: followDocumentEvents turns its 404
 *  (DOCUMENT_NOT_FOUND) into onDeleted, so this 404 is a missing route. 0 (network error), 429 and 5xx are transient. */
export function stopsFollowing(status: number | null): boolean {
  return status === 401 || status === 403 || status === 404
}

/** Reconnect state for every document a page follows. */
export class ReconnectBackoff {
  private readonly failures = new Map<string, number>()
  private readonly stopped = new Set<string>()

  /** A status event arrived, so the stream works: the next early end waits the base delay again. */
  connected(id: string): void {
    this.failures.delete(id)
  }

  /** The stream ended without a terminal event; `status` is the HTTP status it failed with, or null. Returns the wait
   *  before reconnecting, or null when the document must not be followed again. */
  ended(id: string, status: number | null): number | null {
    if (stopsFollowing(status)) {
      this.stopped.add(id)
      return null
    }
    const failures = (this.failures.get(id) ?? 0) + 1
    this.failures.set(id, failures)
    return reconnectDelay(failures)
  }

  isStopped(id: string): boolean {
    return this.stopped.has(id)
  }
}
