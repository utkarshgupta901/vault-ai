import { RefreshCw } from 'lucide-react'
import { useState, type ReactNode } from 'react'
import { isRouteMissing } from '../api/client'
import { getAiUsageMetrics, getChatMetrics, getDocumentMetrics, getProcessingMetrics, getSystemMetrics } from '../api/metrics'
import type { UsageGroupBy } from '../api/types'
import { StatTile } from '../components/charts'
import { AiUsageCard, ChatCard, DocumentsCard, ProcessingCard, SpendMeter, SystemCard } from '../components/metrics/sections'
import { Button, PageHeader, Skeleton } from '../components/ui'
import { useApi, useInterval, type ApiState } from '../hooks/useApi'
import { fmtMs, fmtNumber, fmtPct, fmtUsd } from '../lib/format'

const REFRESH_MS = 10_000

/** A metrics endpoint that refreshes itself every 10 s (paused while the tab is hidden). */
function useMetric<T>(key: string, fetcher: (signal: AbortSignal) => Promise<T>): ApiState<T> {
  const state = useApi(key, fetcher)
  useInterval(state.reload, REFRESH_MS)
  return state
}

export function MetricsPage() {
  const [groupBy, setGroupBy] = useState<UsageGroupBy>('operation')
  const documents = useMetric('metrics:documents', getDocumentMetrics)
  const processing = useMetric('metrics:processing', getProcessingMetrics)
  const usage = useMetric(`metrics:ai-usage:${groupBy}`, (signal) => getAiUsageMetrics(groupBy, signal))
  const chat = useMetric('metrics:chat', getChatMetrics)
  const system = useMetric('metrics:system', getSystemMetrics)
  const all = [documents, processing, usage, chat, system]
  const refreshing = all.some((s) => s.loading && s.data !== undefined)

  return (
    <div className="mx-auto max-w-[1400px] px-4 py-6 lg:px-6">
      <PageHeader
        title="Metrics"
        description="Processing, AI cost, answer quality and API latency. Refreshes every 10 seconds."
        actions={
          <Button icon={RefreshCw} busy={refreshing} onClick={() => all.forEach((s) => s.reload())}>
            Refresh
          </Button>
        }
      />

      <div className="mb-5 grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
        <Kpi state={documents} label="Documents">
          {(d) => <StatTile label="Documents" value={fmtNumber(d.totals.documents)} sub={`${fmtNumber(d.by_status.ready ?? 0)} ready · ${fmtNumber(d.by_status.failed ?? 0)} failed`} />}
        </Kpi>
        <Kpi state={processing} label="Processing success">
          {(p) => <StatTile label="Processing success (24 h)" value={fmtPct(p.success_rate, 1)} sub={`${fmtNumber(p.succeeded)} ok · ${fmtNumber(p.failed)} failed · ${fmtNumber(p.retried)} retried`} />}
        </Kpi>
        <Kpi state={processing} label="Queue">
          {(p) => (
            <StatTile
              label="Queue depth"
              value={fmtNumber(Object.values(p.queue_depth).reduce<number>((sum, n) => sum + (n ?? 0), 0))}
              sub={`${fmtNumber(p.in_flight)} in flight · ${fmtNumber(p.stale)} stale`}
            />
          )}
        </Kpi>
        <Kpi state={usage} label="Spend today">
          {(u) => (
            <div className="rounded-lg border border-zinc-200 bg-white px-4 py-3 shadow-xs">
              <p className="text-xs text-zinc-500">AI spend today</p>
              <SpendMeter usage={u} />
            </div>
          )}
        </Kpi>
        <Kpi state={usage} label="Unit cost">
          {(u) => <StatTile label="Cost per answer" value={fmtUsd(u.cost_per_answer_usd)} sub={`${fmtUsd(u.cost_per_document_usd)} per document`} />}
        </Kpi>
        <Kpi state={chat} label="Time to first token">
          {(c) => <StatTile label="Time to first token" value={fmtMs(c.ttft_ms.p50)} sub={`p95 ${fmtMs(c.ttft_ms.p95)} · ${fmtNumber(c.answers)} answers`} />}
        </Kpi>
      </div>

      <div className="grid gap-5 xl:grid-cols-2">
        <DocumentsCard state={documents} />
        <ProcessingCard state={processing} />
        <AiUsageCard state={usage} groupBy={groupBy} onGroupBy={setGroupBy} />
        <ChatCard state={chat} />
        <SystemCard state={system} />
      </div>
    </div>
  )
}

/** A KPI tile bound to an endpoint; shows a skeleton while loading and a muted placeholder if unavailable. */
function Kpi<T>({ state, label, children }: { state: ApiState<T>; label: string; children: (data: T) => ReactNode }) {
  if (state.data !== undefined) return <>{children(state.data)}</>
  if (state.error) {
    return <StatTile label={label} value={<span className="text-zinc-300">—</span>} sub={isRouteMissing(state.error) ? 'Not available yet' : 'Could not load'} />
  }
  return <Skeleton className="h-[5.5rem] rounded-lg" />
}
