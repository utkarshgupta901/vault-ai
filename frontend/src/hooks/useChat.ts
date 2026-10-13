import { useCallback, useEffect, useRef, useState } from 'react'
import { askJson, askStream, listMessages } from '../api/chat'
import { ApiError, isAbortError } from '../api/client'
import type { CitationsEvent, DoneEvent, ErrorEvent, MessageOut, MetaEvent } from '../api/types'
import { useLatest } from './useApi'
import { useToast } from './useToast'

export interface TurnError {
  code: string
  message: string
  requestId: string | null
}

/**
 * The question currently being answered. It lives outside `messages` because it is built from stream events; once
 * the server has persisted the answer (`done`), the history is re-fetched and the server's copy replaces this one.
 */
export interface LiveTurn {
  clientMessageId: string
  question: string
  stream: boolean
  status: 'streaming' | 'done' | 'error' | 'stopped'
  meta: MetaEvent | null
  content: string
  result: CitationsEvent | null
  followups: string[]
  error: TurnError | null
}

/** Idempotency key for one question: a retry re-sends the same id, so the server regenerates instead of duplicating. */
export function newClientMessageId(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') return crypto.randomUUID()
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`
}

function toTurnError(err: unknown): TurnError {
  if (err instanceof ApiError) return { code: err.code, message: err.message, requestId: err.requestId }
  return { code: 'CLIENT_ERROR', message: err instanceof Error ? err.message : String(err), requestId: null }
}

/** Conversation state for one chat session: history, the live streamed turn, and ask / stop / retry. */
export function useChat(sessionId: string | undefined) {
  const toast = useToast()
  const [messages, setMessages] = useState<MessageOut[]>([])
  const [olderCursor, setOlderCursor] = useState<string | null>(null)
  const [loading, setLoading] = useState(Boolean(sessionId))
  const [loadError, setLoadError] = useState<unknown>(null)
  const [live, setLive] = useState<LiveTurn | null>(null)
  // `done` carries per-step timings that the stored message does not expose; keep them for the "Why" drawer.
  const [doneById, setDoneById] = useState<Record<string, DoneEvent>>({})
  const abortRef = useRef<AbortController | null>(null)
  const liveRef = useLatest(live)

  // Switching session: reset during render (React's "adjust state when a prop changes" pattern), not in an effect,
  // so the previous conversation is never painted under the new session's header.
  const [shownSession, setShownSession] = useState(sessionId)
  if (shownSession !== sessionId) {
    setShownSession(sessionId)
    setMessages([])
    setOlderCursor(null)
    setLive(null)
    setLoadError(null)
    setLoading(Boolean(sessionId))
  }

  const loadLatest = useCallback(
    async (signal?: AbortSignal) => {
      if (!sessionId) return
      const page = await listMessages(sessionId, null, signal)
      setMessages(page.items)
      setOlderCursor(page.next_cursor)
    },
    [sessionId],
  )

  useEffect(() => {
    if (!sessionId) return
    const controller = new AbortController()
    listMessages(sessionId, null, controller.signal)
      .then((page) => {
        setMessages(page.items)
        setOlderCursor(page.next_cursor)
      })
      .catch((err: unknown) => {
        if (!isAbortError(err)) setLoadError(err)
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false)
      })
    return () => {
      controller.abort()
      abortRef.current?.abort() // leaving the session stops its stream (the server saves it as interrupted)
    }
  }, [sessionId])

  const loadOlder = useCallback(async () => {
    if (!sessionId || !olderCursor) return
    try {
      const page = await listMessages(sessionId, olderCursor)
      setMessages((m) => [...page.items, ...m])
      setOlderCursor(page.next_cursor)
    } catch (err) {
      toast.error(err)
    }
  }, [sessionId, olderCursor, toast])

  const ask = useCallback(
    async (question: string, stream: boolean, retryOf?: LiveTurn) => {
      const content = question.trim()
      if (!sessionId || !content || abortRef.current) return
      const clientMessageId = retryOf?.clientMessageId ?? newClientMessageId()
      // A stopped/failed turn was persisted by the server; fold it into the history before starting a new question.
      if (!retryOf && liveRef.current) await loadLatest().catch(() => undefined)

      // Only touch the live turn if it is still this question (the user may have switched session meanwhile).
      const update = (fn: (t: LiveTurn) => LiveTurn | null) =>
        setLive((t) => (t && t.clientMessageId === clientMessageId ? fn(t) : t))
      setLive({
        clientMessageId,
        question: content,
        stream,
        status: 'streaming',
        meta: null,
        content: '',
        result: null,
        followups: [],
        error: null,
      })
      const controller = new AbortController()
      abortRef.current = controller
      try {
        if (stream) await streamAnswer(sessionId, content, clientMessageId, controller.signal, update, setDoneById)
        else await askJson(sessionId, content, clientMessageId, controller.signal)
        update((t) => ({ ...t, status: 'done' }))
        try {
          await loadLatest()
          update(() => null) // the persisted copy is now in `messages`
        } catch (err) {
          toast.error(err, 'Answer saved, but the conversation could not be refreshed')
        }
      } catch (err) {
        if (isAbortError(err)) {
          update((t) => ({ ...t, status: 'stopped' }))
        } else {
          update((t) => ({ ...t, status: 'error', error: toTurnError(err) }))
          toast.error(err)
        }
      } finally {
        if (abortRef.current === controller) abortRef.current = null
      }
    },
    [sessionId, loadLatest, toast, liveRef],
  )

  const stop = useCallback(() => abortRef.current?.abort(), [])

  const retry = useCallback(() => {
    const turn = liveRef.current
    if (turn && turn.status !== 'streaming') void ask(turn.question, turn.stream, turn)
  }, [ask, liveRef])

  return { messages, olderCursor, loading, loadError, live, doneById, ask, stop, retry, loadOlder, reload: loadLatest }
}

/** Reads one streamed answer, folding each event into the live turn. Resolves on `done`, rejects otherwise. */
async function streamAnswer(
  sessionId: string,
  content: string,
  clientMessageId: string,
  signal: AbortSignal,
  update: (fn: (t: LiveTurn) => LiveTurn | null) => void,
  setDoneById: (fn: (d: Record<string, DoneEvent>) => Record<string, DoneEvent>) => void,
): Promise<void> {
  // A holder object (not two `let`s) because the callback below assigns it; TypeScript cannot track closure writes.
  const outcome: { done: boolean; error: ErrorEvent | null; requestId: string | null } = {
    done: false,
    error: null,
    requestId: null,
  }
  await askStream(
    sessionId,
    content,
    clientMessageId,
    (ev) => {
      switch (ev.event) {
        case 'meta':
          update((t) => ({ ...t, meta: ev.data }))
          break
        case 'token':
          update((t) => ({ ...t, content: t.content + ev.data.delta }))
          break
        case 'citations':
          update((t) => ({ ...t, result: ev.data }))
          break
        case 'followups':
          update((t) => ({ ...t, followups: ev.data.questions }))
          break
        case 'done':
          outcome.done = true
          setDoneById((d) => ({ ...d, [ev.data.message_id]: ev.data }))
          break
        case 'error':
          outcome.error = ev.data
          break
      }
    },
    signal,
    (res) => {
      outcome.requestId = res.headers.get('X-Request-ID')
    },
  )
  if (outcome.error) throw new ApiError(200, outcome.error.code, outcome.error.message, {}, outcome.requestId)
  if (!outcome.done) {
    throw new ApiError(0, 'STREAM_INCOMPLETE', 'The connection closed before the answer finished.', {}, outcome.requestId)
  }
}
