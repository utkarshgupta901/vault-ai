import { FileText, Zap } from 'lucide-react'
import { useState, type FormEvent } from 'react'
import { ApiError, isRouteMissing } from '../../api/client'
import { createSummary } from '../../api/documents'
import type { SummaryLength, SummaryOut, SummaryTone } from '../../api/types'
import { Markdown } from '../Markdown'
import { Badge, Button, Card, CardHeader } from '../ui'

const LENGTHS: SummaryLength[] = ['short', 'medium', 'detailed']
const TONES: SummaryTone[] = ['neutral', 'executive', 'technical', 'simple']
const FOCUS_MAX = 200

const control = 'h-9 rounded-md border border-zinc-300 bg-white px-2.5 text-sm text-zinc-800 focus:border-accent-500 focus:outline-none disabled:bg-zinc-50'

/** Custom summary (POST /documents/{id}/summaries). Identical options are served from the cache at no model cost. */
export function SummaryPanel({ documentId, ready }: { documentId: string; ready: boolean }) {
  const [length, setLength] = useState<SummaryLength>('medium')
  const [tone, setTone] = useState<SummaryTone>('neutral')
  const [focus, setFocus] = useState('')
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState<SummaryOut | null>(null)
  const [error, setError] = useState<unknown>(null)

  async function submit(e: FormEvent) {
    e.preventDefault()
    setBusy(true)
    setError(null)
    try {
      setResult(await createSummary(documentId, { length, tone, focus: focus.trim() || null }))
    } catch (err) {
      setError(err)
    } finally {
      setBusy(false)
    }
  }

  return (
    <Card>
      <CardHeader
        title={
          <span className="flex items-center gap-1.5">
            <FileText className="size-4 text-accent-600" aria-hidden /> Custom summary
          </span>
        }
        description="Choose a length, a tone and an optional focus."
      />
      <form onSubmit={submit} className="flex flex-wrap items-end gap-2 px-4 pt-4">
        <label className="flex flex-col gap-1 text-xs text-zinc-500">
          Length
          <select value={length} onChange={(e) => setLength(e.target.value as SummaryLength)} className={control} disabled={!ready}>
            {LENGTHS.map((l) => (
              <option key={l} value={l}>
                {l}
              </option>
            ))}
          </select>
        </label>
        <label className="flex flex-col gap-1 text-xs text-zinc-500">
          Tone
          <select value={tone} onChange={(e) => setTone(e.target.value as SummaryTone)} className={control} disabled={!ready}>
            {TONES.map((t) => (
              <option key={t} value={t}>
                {t}
              </option>
            ))}
          </select>
        </label>
        <label className="flex min-w-48 flex-1 flex-col gap-1 text-xs text-zinc-500">
          Focus (optional)
          <input
            value={focus}
            onChange={(e) => setFocus(e.target.value.slice(0, FOCUS_MAX))}
            placeholder="e.g. termination and liability"
            className={control}
            disabled={!ready}
          />
        </label>
        <Button type="submit" variant="primary" busy={busy} disabled={!ready}>
          Summarise
        </Button>
      </form>
      <div className="p-4">
        {!ready && <p className="text-sm text-zinc-500">Available once the document is ready.</p>}
        {error != null && (
          <p className="rounded-md bg-red-50 px-3 py-2 text-sm text-red-700" role="alert">
            {isRouteMissing(error) ? (
              'Custom summaries are not available on this server yet.'
            ) : (
              <>
                <span className="font-mono text-xs font-medium">{error instanceof ApiError ? error.code : 'ERROR'}</span> · {error instanceof Error ? error.message : String(error)}
                {error instanceof ApiError && error.requestId && <span className="ml-1 font-mono text-xs text-red-400">({error.requestId})</span>}
              </>
            )}
          </p>
        )}
        {result && (
          <div className="rounded-md border border-zinc-200 p-4">
            <div className="mb-3 flex flex-wrap items-center gap-1.5">
              <Badge>{result.length}</Badge>
              <Badge>{result.tone}</Badge>
              {result.focus && <Badge tone="violet">focus: {result.focus}</Badge>}
              {result.cached && (
                <Badge tone="green" icon={Zap} title="Served from the summary cache: no model call, no cost.">
                  Cached
                </Badge>
              )}
              <span className="ml-auto text-xs text-zinc-400">
                {result.model} · {result.prompt_version}
              </span>
            </div>
            <Markdown text={result.content} />
          </div>
        )}
      </div>
    </Card>
  )
}
