/**
 * GET /health/ready for the header's health dot. Split from metrics.ts the same way sseParser.ts is split from sse.ts:
 * no value imports, so `pnpm test` can run it under node with a stubbed fetch.
 */
import type { ReadyResponse } from './types'

/** A healthy probe takes ~1 s (the Celery worker check waits up to 1 s for replies), so 5 s is ample headroom, and it
 *  is well under the 15 s poll. Without a timeout a hung probe never settles: the last "ok" stayed on screen until a
 *  later poll happened to abort it (up to 30 s), and a first page load sat on "Checking…". */
export const READY_TIMEOUT_MS = 5_000

/**
 * /health/ready answers 503 *with a JSON body* when a hard dependency is down, so the body is read for any status
 * instead of treating 503 as an exception. Returns null ("unreachable") on a network error, a timeout, or a body that
 * is not a readiness report (e.g. a proxy's HTML error page).
 *
 * When the CALLER aborts (a newer poll, a manual re-check, unmount) the abort is rethrown rather than turned into
 * null: useApi ignores AbortErrors, whereas null would flash "unavailable" for a request nobody is waiting for.
 */
export async function fetchReadiness(
  headers: HeadersInit,
  signal?: AbortSignal,
  timeoutMs = READY_TIMEOUT_MS,
): Promise<ReadyResponse | null> {
  const timeout = AbortSignal.timeout(timeoutMs)
  try {
    // The signal also covers reading the body, so a server that sends headers and then stalls is cut off too.
    const res = await fetch('/health/ready', { headers, signal: signal ? AbortSignal.any([signal, timeout]) : timeout })
    return asReadyResponse(await res.json())
  } catch (err) {
    if (signal?.aborted) throw err
    return null
  }
}

const STATUSES: ReadonlySet<unknown> = new Set(['ok', 'degraded', 'unavailable'])

/** The indicator indexes its colours by `status` and iterates `checks`; anything else counts as "unreachable". */
function asReadyResponse(body: unknown): ReadyResponse | null {
  if (typeof body !== 'object' || body === null) return null
  const { status, checks } = body as Partial<ReadyResponse>
  return STATUSES.has(status) && typeof checks === 'object' && checks !== null ? (body as ReadyResponse) : null
}
