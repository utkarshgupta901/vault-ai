import { AlertCircle, CircleSlash, Info, RotateCw, SearchX, ShieldAlert, ShieldCheck, ShieldQuestion, Sparkles, User } from 'lucide-react'
import { useMemo } from 'react'
import type { Confidence } from '../../api/types'
import { isUnverified, rewriteDiffers, type AnswerView } from '../../lib/answers'
import { buildCitationLookup, splitIncompleteMarker, type CitationInfo } from '../../lib/citations'
import { cx } from '../../lib/cx'
import { fmtMs, fmtUsd } from '../../lib/format'
import { Markdown } from '../Markdown'
import { Badge, Button, type Tone } from '../ui'
import { WhyThisAnswer } from './WhyThisAnswer'

export function UserMessage({ content }: { content: string }) {
  return (
    <div className="flex justify-end gap-3">
      <div className="max-w-[80%] rounded-2xl rounded-tr-sm bg-accent-600 px-4 py-2.5 text-[0.9375rem] whitespace-pre-wrap text-white">{content}</div>
      <div className="grid size-7 shrink-0 place-items-center rounded-full bg-zinc-200 text-zinc-600" aria-hidden>
        <User className="size-4" />
      </div>
    </div>
  )
}

/** Plain-language explanations of the server's warning codes (design §2b); unknown codes are shown as-is. */
const WARNING_TEXT: Record<string, string> = {
  fallback_model: 'The main model was slow to start, so the fast fallback model answered.',
  keyword_only: 'Query embedding timed out; keyword-only retrieval was used.',
  condense_fallback: 'Rewriting the follow-up timed out; your question was used as typed.',
  docs_not_ready: 'Some documents in this chat are still processing and were not searched.',
}

const CONFIDENCE: Record<Confidence, { tone: Tone; icon: typeof ShieldCheck; label: string }> = {
  high: { tone: 'green', icon: ShieldCheck, label: 'High confidence' },
  medium: { tone: 'amber', icon: ShieldQuestion, label: 'Medium confidence' },
  low: { tone: 'red', icon: ShieldAlert, label: 'Low confidence' },
}

interface AssistantMessageProps {
  view: AnswerView
  /** Follow-up chips are offered on the latest answer only. */
  isLatest: boolean
  onAsk: (question: string) => void
  onOpenPage: (info: CitationInfo) => void
  onRetry?: () => void
}

export function AssistantMessage({ view, isLatest, onAsk, onOpenPage, onRetry }: AssistantMessageProps) {
  const streaming = view.state === 'streaming'
  const resolve = useMemo(() => buildCitationLookup(view.citations, view.sources), [view.citations, view.sources])
  // While streaming, hold back a half-received "[S1" so it never flashes as text before becoming a chip.
  const text = streaming ? splitIncompleteMarker(view.content).stable : view.content
  const notFound = view.answerType === 'not_found'
  const unverified = isUnverified(view)
  const signals = view.signals

  return (
    <div className="flex gap-3">
      <div className="grid size-7 shrink-0 place-items-center rounded-full bg-blue-900 text-white" aria-hidden>
        <Sparkles className="size-3.5" />
      </div>
      <div className="min-w-0 flex-1">
        {rewriteDiffers(view.question, view.standaloneQuery) && (
          <p className="mb-1.5 text-xs text-zinc-500">
            Interpreted as: <span className="text-zinc-700 italic">“{view.standaloneQuery}”</span>
          </p>
        )}
        <div className={cx('rounded-2xl rounded-tl-sm border px-4 py-3', notFound ? 'border-dashed border-zinc-300 bg-zinc-50' : 'border-zinc-200 bg-white')}>
          {notFound && (
            <p className="mb-2 flex items-center gap-1.5 text-sm font-medium text-zinc-700">
              <SearchX className="size-4 text-zinc-500" aria-hidden /> Not found in your documents
            </p>
          )}
          {unverified && (
            <div className="mb-3 flex gap-2 rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-900" role="note">
              <AlertCircle className="mt-0.5 size-4 shrink-0" aria-hidden />
              <span>
                <span className="font-semibold">Unverified.</span> Parts of this answer could not be matched to the cited sources
                {signals?.invalid_citations ? ` (${signals.invalid_citations} invalid citation${signals.invalid_citations > 1 ? 's' : ''})` : ''}
                {signals?.ungrounded_facts ? ` (${signals.ungrounded_facts} number/code not found in the sources)` : ''}. Check the sources before relying on it.
              </span>
            </div>
          )}
          {text ? (
            <div className={cx(notFound && 'text-zinc-600', streaming && 'is-streaming')}>
              <Markdown text={text} resolveCitation={resolve} onOpenPage={onOpenPage} />
            </div>
          ) : streaming ? (
            <ThinkingDots />
          ) : null}

          <AnswerFooter view={view} />
          <StateNotes view={view} onRetry={onRetry} />
          {!streaming && view.state !== 'error' && <WhyThisAnswer view={view} />}
          {streaming && view.sources.length > 0 && <WhyThisAnswer view={view} />}
        </div>

        {isLatest && !streaming && view.followups.length > 0 && (
          <div className="mt-2 flex flex-wrap gap-1.5" aria-label="Follow-up questions">
            {view.followups.map((q) => (
              <button key={q} type="button" onClick={() => onAsk(q)} className="rounded-full border border-zinc-200 bg-white px-3 py-1 text-left text-xs text-zinc-700 hover:border-accent-500 hover:text-accent-700">
                {q}
              </button>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}

function ThinkingDots() {
  return (
    <span className="inline-flex items-center gap-1 py-1" role="status" aria-label="Retrieving sources">
      {[0, 150, 300].map((delay) => (
        <span key={delay} className="size-1.5 animate-bounce rounded-full bg-zinc-400" style={{ animationDelay: `${delay}ms` }} />
      ))}
      <span className="ml-2 text-xs text-zinc-500">Searching your documents…</span>
    </span>
  )
}

function AnswerFooter({ view }: { view: AnswerView }) {
  const conf = view.confidence ? CONFIDENCE[view.confidence] : null
  const gate = view.signals?.gate
  const showMeta = view.state !== 'streaming' && (conf || view.latencyMs != null || view.costUsd != null)
  return (
    <>
      {view.warnings.length > 0 && (
        <ul className="mt-3 space-y-1">
          {view.warnings.map((w) => (
            <li key={w} className="flex items-start gap-1.5 text-xs text-zinc-500">
              <Info className="mt-0.5 size-3.5 shrink-0" aria-hidden />
              <span>
                {WARNING_TEXT[w] ?? w} <span className="font-mono text-zinc-400">({w})</span>
              </span>
            </li>
          ))}
        </ul>
      )}
      {gate && (
        <p className="mt-2 text-xs text-zinc-500">Refused by the retrieval gate: no source was similar enough, so no model call was made ($0).</p>
      )}
      {showMeta && (
        <div className="mt-3 flex flex-wrap items-center gap-1.5 text-xs text-zinc-500">
          {conf && (
            <Badge tone={conf.tone} icon={conf.icon} title="Assigned by deterministic checks: retrieval similarity, citation validity and grounding.">
              {conf.label}
            </Badge>
          )}
          {view.citations.length > 0 && <Badge>{view.citations.length} source{view.citations.length > 1 ? 's' : ''}</Badge>}
          <span className="ml-auto flex gap-2 tabular-nums">
            {view.ttftMs != null && <span title="Time to first token">TTFT {fmtMs(view.ttftMs)}</span>}
            {view.latencyMs != null && <span title="Total latency">{fmtMs(view.latencyMs)}</span>}
            {view.costUsd != null && <span title="Model cost for this answer">{fmtUsd(view.costUsd)}</span>}
          </span>
        </div>
      )}
    </>
  )
}

function StateNotes({ view, onRetry }: { view: AnswerView; onRetry?: () => void }) {
  if (view.state === 'error' && view.error) {
    return (
      <div className="mt-3 flex flex-wrap items-center gap-2 rounded-md bg-red-50 px-3 py-2 text-xs text-red-800" role="alert">
        <AlertCircle className="size-4 shrink-0" aria-hidden />
        <span className="min-w-0 flex-1">
          <span className="font-mono font-semibold">{view.error.code}</span> · {view.error.message}
          {view.error.requestId && <span className="ml-1 font-mono text-red-400">({view.error.requestId})</span>}
        </span>
        {onRetry && (
          <Button size="sm" icon={RotateCw} onClick={onRetry}>
            Retry
          </Button>
        )}
      </div>
    )
  }
  if (view.state === 'stopped' || view.state === 'interrupted' || view.state === 'failed') {
    const label = view.state === 'failed' ? 'This answer failed before it finished.' : 'Stopped before the answer finished.'
    return (
      <div className="mt-3 flex items-center gap-2 text-xs text-zinc-500">
        <CircleSlash className="size-3.5" aria-hidden />
        {label}
        {onRetry && (
          <Button size="sm" variant="ghost" icon={RotateCw} onClick={onRetry}>
            Retry
          </Button>
        )}
      </div>
    )
  }
  return null
}
