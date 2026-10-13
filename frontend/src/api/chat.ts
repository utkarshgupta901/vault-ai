/** Typed calls for /api/v1/chat/* (app/schemas/chat.py), including the streaming ask. */
import { request } from './client'
import { openEventStream, parseData, type SSEMessage } from './sse'
import type {
  AskResponse,
  CitationsEvent,
  DoneEvent,
  ErrorEvent,
  FollowupsEvent,
  MessageOut,
  MetaEvent,
  Page,
  SessionCreate,
  SessionOut,
  TokenEvent,
} from './types'

const BASE = '/api/v1/chat/sessions'

export function listSessions(signal?: AbortSignal) {
  return request<Page<SessionOut>>(BASE, { query: { limit: 100 }, signal })
}

export function getSession(id: string, signal?: AbortSignal) {
  return request<SessionOut>(`${BASE}/${id}`, { signal })
}

export function createSession(body: SessionCreate) {
  return request<SessionOut>(BASE, { method: 'POST', json: body })
}

export function deleteSession(id: string) {
  return request<void>(`${BASE}/${id}`, { method: 'DELETE' })
}

/** Newest `limit` messages in chronological (seq) order; next_cursor pages to older ones. */
export function listMessages(id: string, cursor: string | null = null, signal?: AbortSignal) {
  return request<Page<MessageOut>>(`${BASE}/${id}/messages`, { query: { limit: 50, cursor }, signal })
}

/** Non-streaming ask (JSON). The same client_message_id makes a retry idempotent on the server. Aborting `signal`
 *  (the Stop button) only stops waiting: the server may still finish and store the answer. */
export function askJson(id: string, content: string, clientMessageId: string, signal?: AbortSignal) {
  return request<AskResponse>(`${BASE}/${id}/messages`, {
    method: 'POST',
    json: { content, client_message_id: clientMessageId },
    signal,
  })
}

/** The chat stream's events, as a discriminated union so handlers get typed payloads. */
export type ChatStreamEvent =
  | { event: 'meta'; data: MetaEvent }
  | { event: 'token'; data: TokenEvent }
  | { event: 'citations'; data: CitationsEvent }
  | { event: 'followups'; data: FollowupsEvent }
  | { event: 'done'; data: DoneEvent }
  | { event: 'error'; data: ErrorEvent }

const CHAT_EVENTS = new Set(['meta', 'token', 'citations', 'followups', 'done', 'error'])

function toChatEvent(msg: SSEMessage): ChatStreamEvent | null {
  if (!CHAT_EVENTS.has(msg.event)) return null // unknown events are ignored (forward-compatible contract)
  return { event: msg.event, data: parseData(msg) } as ChatStreamEvent
}

/** POST .../messages/stream: meta -> token* -> citations -> followups? -> done (or a terminal error event). */
export function askStream(
  id: string,
  content: string,
  clientMessageId: string,
  onEvent: (ev: ChatStreamEvent) => void,
  signal: AbortSignal,
  onOpen?: (res: Response) => void,
) {
  return openEventStream(
    `${BASE}/${id}/messages/stream`,
    { method: 'POST', json: { content, client_message_id: clientMessageId }, signal },
    (msg) => {
      const ev = toChatEvent(msg)
      if (ev) onEvent(ev)
    },
    onOpen,
  )
}
