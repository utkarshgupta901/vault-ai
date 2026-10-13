/**
 * One view model for an assistant answer, whether it comes from a stored message (GET .../messages) or from the
 * live stream (meta/token/citations/followups/done events). The answer component renders only this shape, so the
 * streamed and the reloaded versions of an answer look identical.
 */
import type {
  AnswerType,
  Citation,
  Confidence,
  DoneEvent,
  MessageOut,
  Money,
  RetrievalInfo,
  Signals,
  SourceRef,
} from '../api/types'
import type { LiveTurn, TurnError } from '../hooks/useChat'

export type AnswerState = 'streaming' | 'complete' | 'interrupted' | 'failed' | 'stopped' | 'error'

export interface AnswerView {
  key: string
  messageId: string | null
  state: AnswerState
  question: string | null
  content: string
  standaloneQuery: string | null
  answerType: AnswerType | null
  confidence: Confidence | null
  signals: Partial<Signals> | null
  citations: Citation[]
  sources: SourceRef[]
  followups: string[]
  warnings: string[]
  retrieval: RetrievalInfo | null
  retrievalMode: string | null
  model: string | null
  promptVersion: string | null
  inputTokens: number | null
  cachedInputTokens: number | null
  outputTokens: number | null
  costUsd: Money | null
  usageEstimated: boolean
  ttftMs: number | null
  latencyMs: number | null
  timings: Record<string, number> | null
  error: TurnError | null
}

/** Warnings are free-form on MessageOut (list[Any]); accept plain codes or {code: ...} objects. */
export function warningCodes(warnings: unknown[] | undefined): string[] {
  return (warnings ?? []).map((w) => {
    if (typeof w === 'string') return w
    if (w && typeof w === 'object' && 'code' in w) return String((w as { code: unknown }).code)
    return JSON.stringify(w)
  })
}

export function fromMessage(msg: MessageOut, question: string | null, done?: DoneEvent): AnswerView {
  return {
    key: msg.id,
    messageId: msg.id,
    state: msg.status,
    question,
    content: msg.content,
    standaloneQuery: msg.standalone_query,
    answerType: msg.answer_type,
    confidence: msg.confidence,
    signals: msg.signals,
    citations: msg.citations ?? [],
    sources: [],
    followups: msg.followups ?? [],
    warnings: warningCodes(msg.warnings),
    retrieval: msg.retrieval,
    retrievalMode: msg.retrieval?.mode ?? null,
    model: msg.model,
    promptVersion: msg.prompt_version,
    inputTokens: msg.input_tokens,
    cachedInputTokens: done?.usage.cached_input_tokens ?? null,
    outputTokens: msg.output_tokens,
    costUsd: msg.cost_usd,
    usageEstimated: done?.usage.estimated ?? false,
    ttftMs: msg.ttft_ms,
    latencyMs: msg.latency_ms,
    timings: msg.timings ?? done?.timings ?? null,
    error: null,
  }
}

export function fromLive(turn: LiveTurn, done?: DoneEvent): AnswerView {
  const state: AnswerState = turn.status === 'done' ? 'complete' : turn.status
  return {
    key: `live-${turn.clientMessageId}`,
    messageId: turn.meta?.message_id ?? null,
    state,
    question: turn.question,
    content: turn.content,
    standaloneQuery: turn.meta?.standalone_query ?? null,
    answerType: turn.result?.answer_type ?? null,
    confidence: turn.result?.confidence ?? null,
    signals: turn.result?.signals ?? null,
    citations: turn.result?.citations ?? [],
    sources: turn.meta?.sources ?? [],
    followups: turn.followups,
    warnings: turn.meta?.warnings ?? [],
    retrieval: null,
    retrievalMode: turn.meta?.retrieval_mode ?? null,
    model: done?.model ?? null,
    promptVersion: null,
    inputTokens: done?.usage.input_tokens ?? null,
    cachedInputTokens: done?.usage.cached_input_tokens ?? null,
    outputTokens: done?.usage.output_tokens ?? null,
    costUsd: done?.usage.cost_usd ?? null,
    usageEstimated: done?.usage.estimated ?? false,
    ttftMs: done?.ttft_ms ?? null,
    latencyMs: done?.latency_ms ?? null,
    timings: done?.timings ?? null,
    error: turn.error,
  }
}

/** Show "Interpreted as" only when the rewrite actually changed the question (ignoring case and punctuation). */
export function rewriteDiffers(question: string | null, standalone: string | null): boolean {
  if (!question || !standalone) return false
  const norm = (s: string) => s.toLowerCase().replace(/[^\p{L}\p{N}]+/gu, ' ').trim()
  return norm(question) !== norm(standalone)
}

/** "Unverified" when the checks found ungrounded content or the model cited ids it was never given. */
export function isUnverified(view: Pick<AnswerView, 'answerType' | 'signals'>): boolean {
  return view.answerType === 'ungrounded' || (view.signals?.invalid_citations ?? 0) > 0
}

/** Pairs each assistant message with the user question it answers (reply_to_id, else the previous user turn). */
export function questionFor(messages: MessageOut[], index: number): string | null {
  const msg = messages[index]
  if (msg.reply_to_id) {
    const parent = messages.find((m) => m.id === msg.reply_to_id)
    if (parent) return parent.content
  }
  for (let i = index - 1; i >= 0; i--) if (messages[i].role === 'user') return messages[i].content
  return null
}
