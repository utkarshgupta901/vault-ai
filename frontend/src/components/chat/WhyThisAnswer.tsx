import { Check, ChevronRight } from 'lucide-react'
import type { ReactNode } from 'react'
import type { RetrievalCandidate } from '../../api/types'
import type { AnswerView } from '../../lib/answers'
import { fmtMs, fmtNumber, fmtScore, fmtUsd, humanize } from '../../lib/format'
import { Badge } from '../ui'

const TIMING_ORDER = ['condense', 'embed', 'retrieve', 'ttft', 'total']

/** similarity = 1 - cosine distance (the vector leg stores the distance pgvector returns). */
function similarity(c: RetrievalCandidate): number | null {
  if (c.similarity != null) return c.similarity
  return c.sem_dist != null ? 1 - c.sem_dist : null
}

/**
 * The retrieval debug drawer: which sources the model saw, how each candidate ranked in the vector and keyword legs,
 * how RRF fused them, and what the answer cost in time and money. A native <details> element: collapsible and
 * keyboard-accessible with no state to manage.
 */
export function WhyThisAnswer({ view }: { view: AnswerView }) {
  const sourceByChunk = new Map(
    [...view.sources, ...view.citations].map((s) => [s.chunk_id, { filename: s.filename, label: s.label }]),
  )
  const candidates = view.retrieval?.candidates ?? []
  const timings = orderedTimings(view)
  const signals = view.signals

  return (
    <details className="group mt-3 rounded-md border border-zinc-200 bg-zinc-50/60 text-sm open:bg-white">
      <summary className="flex cursor-pointer list-none items-center gap-1 px-3 py-2 text-xs font-medium text-zinc-600 select-none hover:text-zinc-900 [&::-webkit-details-marker]:hidden">
        <ChevronRight className="size-3.5 transition-transform group-open:rotate-90" aria-hidden />
        Why this answer?
      </summary>
      <div className="space-y-4 border-t border-zinc-200 px-3 py-3">
        <Block title="Retrieval">
          <div className="flex flex-wrap gap-1.5 text-xs">
            <Badge>mode: {view.retrieval?.mode ?? view.retrievalMode ?? '—'}</Badge>
            {(view.retrieval?.top_similarity ?? signals?.top_similarity) != null && (
              <Badge>top similarity {fmtScore(view.retrieval?.top_similarity ?? signals?.top_similarity)}</Badge>
            )}
            {(view.retrieval?.gate ?? signals?.gate) && <Badge tone="amber">gate: refused before the model</Badge>}
            {signals?.id_hit && <Badge tone="violet">identifier match</Badge>}
            {signals?.lexical_match && <Badge>keyword match</Badge>}
          </div>
          {(view.retrieval?.terms?.length ?? 0) > 0 && (
            <p className="mt-2 text-xs text-zinc-500">
              Keyword terms: <span className="font-mono text-zinc-700">{view.retrieval?.terms?.join(' | ')}</span>
              {(view.retrieval?.id_terms?.length ?? 0) > 0 && (
                <>
                  {' '}
                  · identifiers: <span className="font-mono text-zinc-700">{view.retrieval?.id_terms?.join(', ')}</span>
                </>
              )}
            </p>
          )}
        </Block>

        {candidates.length > 0 ? (
          <Block title={`Candidates (${candidates.length})`} note="Reciprocal rank fusion of the vector and keyword legs; sources S1… were sent to the model.">
            <div className="overflow-x-auto">
              <table className="w-full min-w-[560px] text-xs tabular-nums">
                <thead>
                  <tr className="text-left text-zinc-500">
                    <th className="py-1 pr-2 font-medium">Ref</th>
                    <th className="py-1 pr-2 font-medium">Chunk</th>
                    <th className="py-1 pr-2 text-right font-medium">RRF</th>
                    <th className="py-1 pr-2 text-right font-medium">Vector rank</th>
                    <th className="py-1 pr-2 text-right font-medium">Keyword rank</th>
                    <th className="py-1 pr-2 text-right font-medium">Similarity</th>
                    <th className="py-1 text-center font-medium">ID hit</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-zinc-100">
                  {candidates.map((c) => {
                    const src = sourceByChunk.get(c.chunk_id)
                    return (
                      <tr key={c.chunk_id} className={c.ref ? 'text-zinc-900' : 'text-zinc-400'}>
                        <td className="py-1 pr-2 font-mono">{c.ref ?? '—'}</td>
                        <td className="max-w-48 truncate py-1 pr-2" title={src ? `${src.filename} ${src.label}` : undefined}>
                          {src ? `${src.filename} · ${src.label}` : c.filename ? `${c.filename} · ${c.label ?? ''}` : `#${c.chunk_id}`}
                        </td>
                        <td className="py-1 pr-2 text-right">{fmtScore(c.rrf, 4)}</td>
                        <td className="py-1 pr-2 text-right">{c.sem_rank ?? '—'}</td>
                        <td className="py-1 pr-2 text-right">{c.lex_rank ?? '—'}</td>
                        <td className="py-1 pr-2 text-right">{fmtScore(similarity(c))}</td>
                        <td className="py-1 text-center">{c.id_hit ? <Check className="mx-auto size-3.5 text-violet-600" aria-label="yes" /> : ''}</td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
          </Block>
        ) : view.sources.length > 0 || view.citations.length > 0 ? (
          <Block title="Sources" note={view.state === 'streaming' ? 'Sent to the model for this answer. Candidate scores appear when the answer completes.' : undefined}>
            <ul className="space-y-1 text-xs">
              {(view.sources.length ? view.sources : view.citations).map((s) => (
                <li key={s.ref} className="flex gap-2">
                  <span className="w-7 shrink-0 font-mono text-zinc-500">{s.ref}</span>
                  <span className="truncate text-zinc-800">
                    {s.filename} · {s.label}
                  </span>
                </li>
              ))}
            </ul>
          </Block>
        ) : null}

        {signals && (
          <Block title="Grounding checks">
            <div className="flex flex-wrap gap-1.5 text-xs">
              <Badge tone="green">{fmtNumber(signals.valid_citations ?? 0)} valid citations</Badge>
              <Badge tone={signals.invalid_citations ? 'red' : 'neutral'}>{fmtNumber(signals.invalid_citations ?? 0)} invalid</Badge>
              <Badge tone={signals.uncited_claims ? 'amber' : 'neutral'}>{fmtNumber(signals.uncited_claims ?? 0)} uncited claims</Badge>
              <Badge tone={signals.ungrounded_facts ? 'amber' : 'neutral'}>{fmtNumber(signals.ungrounded_facts ?? 0)} ungrounded numbers/codes</Badge>
            </div>
          </Block>
        )}

        {(timings.length > 0 || view.model || view.costUsd != null) && (
          <Block title="Cost and timing">
            <dl className="grid grid-cols-3 gap-x-4 gap-y-2 text-xs sm:grid-cols-5">
              {timings.map(([k, v]) => (
                <Stat key={k} label={k === 'ttft' ? 'First token' : humanize(k)} value={fmtMs(v)} />
              ))}
              {view.model && <Stat label="Model" value={view.model} />}
              {view.inputTokens != null && <Stat label="Input tokens" value={`${fmtNumber(view.inputTokens)}${view.cachedInputTokens ? ` (${fmtNumber(view.cachedInputTokens)} cached)` : ''}`} />}
              {view.outputTokens != null && <Stat label="Output tokens" value={fmtNumber(view.outputTokens)} />}
              {view.costUsd != null && <Stat label={view.usageEstimated ? 'Cost (estimated)' : 'Cost'} value={fmtUsd(view.costUsd)} />}
              {view.promptVersion && <Stat label="Prompt" value={view.promptVersion} />}
            </dl>
          </Block>
        )}
      </div>
    </details>
  )
}

/** Per-step timings in pipeline order; falls back to the stored ttft/latency when the step breakdown is unavailable. */
function orderedTimings(view: AnswerView): [string, number][] {
  const t: Record<string, number> = { ...(view.timings ?? {}) }
  if (t.ttft == null && view.ttftMs != null) t.ttft = view.ttftMs
  if (t.total == null && view.latencyMs != null) t.total = view.latencyMs
  const known = TIMING_ORDER.filter((k) => t[k] != null).map((k) => [k, t[k]] as [string, number])
  const extra = Object.entries(t).filter(([k]) => !TIMING_ORDER.includes(k))
  return [...known, ...extra]
}

function Block({ title, note, children }: { title: string; note?: string; children: ReactNode }) {
  return (
    <section>
      <h5 className="text-[0.7rem] font-semibold tracking-wide text-zinc-500 uppercase">{title}</h5>
      {note && <p className="mt-0.5 text-xs text-zinc-400">{note}</p>}
      <div className="mt-1.5">{children}</div>
    </section>
  )
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="min-w-0">
      <dt className="text-zinc-500">{label}</dt>
      <dd className="truncate font-medium text-zinc-900 tabular-nums" title={value}>
        {value}
      </dd>
    </div>
  )
}
