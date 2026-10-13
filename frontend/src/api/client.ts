/**
 * The one place that talks HTTP. Every request:
 * - uses a relative URL (same origin: FastAPI serves this SPA in production, Vite proxies /api in dev);
 * - sends the X-API-Key from localStorage (the server scopes every query to that key's owner);
 * - turns a non-2xx response into an ApiError parsed from the error envelope
 *   {error: {code, message, details, request_id}}, so the UI can always show code + message + request id.
 */
import type { ErrorEnvelope } from './types'

const API_KEY_STORAGE = 'vault.apiKey'
export const DEFAULT_API_KEY = 'vault-demo-key'

/** localStorage can throw (private mode, blocked storage); the demo key is a safe fallback. */
export function getApiKey(): string {
  try {
    return localStorage.getItem(API_KEY_STORAGE) || DEFAULT_API_KEY
  } catch {
    return DEFAULT_API_KEY
  }
}

export function setApiKey(key: string): void {
  try {
    localStorage.setItem(API_KEY_STORAGE, key.trim() || DEFAULT_API_KEY)
  } catch {
    // Storage unavailable: the key falls back to the default on the next read.
  }
}

export class ApiError extends Error {
  readonly status: number
  readonly code: string
  readonly details: Record<string, unknown>
  readonly requestId: string | null

  constructor(status: number, code: string, message: string, details: Record<string, unknown> = {}, requestId: string | null = null) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
    this.details = details
    this.requestId = requestId
  }
}

export function isAbortError(err: unknown): boolean {
  return err instanceof DOMException && err.name === 'AbortError'
}

/** A 404 whose code is the generic NOT_FOUND means the *route* does not exist on this server (a work package not
 *  merged yet), as opposed to DOCUMENT_NOT_FOUND / SESSION_NOT_FOUND for a missing resource. */
export function isRouteMissing(err: unknown): boolean {
  return err instanceof ApiError && err.status === 404 && err.code === 'NOT_FOUND'
}

export function authHeaders(extra: Record<string, string> = {}): Headers {
  return new Headers({ 'X-API-Key': getApiKey(), ...extra })
}

/** Builds an ApiError from any failed response, preferring the envelope and falling back to status text. */
export async function toApiError(res: Response): Promise<ApiError> {
  const headerRequestId = res.headers.get('X-Request-ID')
  try {
    const body = (await res.json()) as Partial<ErrorEnvelope>
    if (body.error?.code) {
      const e = body.error
      return new ApiError(res.status, e.code, e.message, e.details ?? {}, e.request_id ?? headerRequestId)
    }
  } catch {
    // Not JSON (e.g. a proxy error page): fall through.
  }
  return new ApiError(res.status, `HTTP_${res.status}`, res.statusText || 'Request failed', {}, headerRequestId)
}

export function networkError(): ApiError {
  return new ApiError(0, 'NETWORK_ERROR', 'Cannot reach the Vault API. Is the server running?')
}

type QueryValue = string | number | boolean | null | undefined
export type Query = Record<string, QueryValue | QueryValue[]>

export function withQuery(path: string, query?: Query): string {
  if (!query) return path
  const params = new URLSearchParams()
  for (const [key, value] of Object.entries(query)) {
    for (const v of Array.isArray(value) ? value : [value]) {
      if (v !== undefined && v !== null && v !== '') params.append(key, String(v))
    }
  }
  const qs = params.toString()
  return qs ? `${path}?${qs}` : path
}

export interface RequestOptions {
  method?: string
  query?: Query
  /** JSON body (sets Content-Type). */
  json?: unknown
  /** Raw body, e.g. FormData for uploads (the browser sets the multipart boundary and Content-Length). */
  body?: BodyInit
  signal?: AbortSignal
  accept?: string
}

/** fetch() with auth headers; rejects with ApiError for network failures and non-2xx responses. */
export async function send(path: string, opts: RequestOptions = {}): Promise<Response> {
  const headers = authHeaders({ Accept: opts.accept ?? 'application/json' })
  let body = opts.body
  if (opts.json !== undefined) {
    headers.set('Content-Type', 'application/json')
    body = JSON.stringify(opts.json)
  }
  let res: Response
  try {
    res = await fetch(withQuery(path, opts.query), { method: opts.method ?? 'GET', headers, body, signal: opts.signal })
  } catch (err) {
    if (isAbortError(err)) throw err
    throw networkError()
  }
  if (!res.ok) throw await toApiError(res)
  return res
}

/** JSON request helper. 204 No Content resolves to undefined. */
export async function request<T>(path: string, opts: RequestOptions = {}): Promise<T> {
  const res = await send(path, opts)
  if (res.status === 204) return undefined as T
  // In production FastAPI answers unknown GET paths with the SPA's index.html (200, text/html) so client-side routes
  // survive a refresh. For an API call that means "this route is not deployed": report it as a missing route instead
  // of failing later with a JSON parse error.
  if (!res.headers.get('Content-Type')?.includes('json')) {
    throw new ApiError(404, 'NOT_FOUND', `${path.split('?')[0]} is not available on this server.`, {}, res.headers.get('X-Request-ID'))
  }
  return (await res.json()) as T
}
