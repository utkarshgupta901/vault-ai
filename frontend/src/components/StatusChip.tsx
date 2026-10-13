import { AlertCircle, CheckCircle2, Clock, Loader2, Sparkles, TriangleAlert } from 'lucide-react'
import type { DocumentError, DocumentStatus, InsightsStatus } from '../api/types'
import { humanize } from '../lib/format'
import { Badge, ProgressBar } from './ui'

interface StatusChipProps {
  status: DocumentStatus
  stage: string | null
  progress: number
  stale?: boolean
  error?: DocumentError | null
  /** Show the stage + progress bar under the chip while processing (tables); off for compact headers. */
  detailed?: boolean
}

/** Document processing status. Colour is never the only signal: every state has its own icon and label. */
export function StatusChip({ status, stage, progress, stale, error, detailed = true }: StatusChipProps) {
  if (status === 'ready') return <Badge tone="green" icon={CheckCircle2}>Ready</Badge>
  if (status === 'failed') {
    return (
      <div className="min-w-0">
        <Badge tone="red" icon={AlertCircle} title={error?.message}>
          Failed{error ? ` · ${error.code}` : ''}
        </Badge>
        {detailed && error && <p className="mt-1 max-w-64 text-xs text-red-700/80">{error.message}</p>}
      </div>
    )
  }
  const queued = status === 'queued'
  return (
    <div className="min-w-32">
      <div className="flex items-center gap-1.5">
        <Badge tone="blue" icon={queued ? Clock : Loader2} className={queued ? undefined : '[&>svg]:animate-spin'}>
          {queued ? 'Queued' : stage ? humanize(stage) : 'Processing'}
        </Badge>
        {!queued && <span className="text-xs text-zinc-500 tabular-nums">{progress}%</span>}
        {stale && <Badge tone="amber" icon={TriangleAlert} title="No progress for a while; it can be reprocessed.">Stale</Badge>}
      </div>
      {detailed && !queued && <ProgressBar value={progress} className="mt-1.5" />}
    </div>
  )
}

const INSIGHTS: Record<InsightsStatus, { tone: 'neutral' | 'blue' | 'green' | 'red'; label: string }> = {
  pending: { tone: 'neutral', label: 'Pending' },
  processing: { tone: 'blue', label: 'Generating' },
  ready: { tone: 'green', label: 'Ready' },
  failed: { tone: 'red', label: 'Failed' },
}

export function InsightsChip({ status, errorCode }: { status: InsightsStatus; errorCode?: string | null }) {
  const { tone, label } = INSIGHTS[status]
  const Icon = status === 'processing' ? Loader2 : status === 'failed' ? AlertCircle : Sparkles
  return (
    <Badge tone={tone} icon={Icon} title={errorCode ?? undefined} className={status === 'processing' ? '[&>svg]:animate-spin' : undefined}>
      {label}
      {status === 'failed' && errorCode ? ` · ${errorCode}` : ''}
    </Badge>
  )
}
