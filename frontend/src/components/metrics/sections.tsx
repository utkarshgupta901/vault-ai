/** One component per metrics endpoint. Each renders the frozen response shape from app/schemas/metrics.py. */
import type { AIUsageMetrics, ChatMetrics, DocumentStats, Percentiles, ProcessingMetrics, SystemMetrics, UsageGroupBy } from '../../api/types'
import type { ApiState } from '../../hooks/useApi'
import { recordToBars } from '../../lib/bars'
import { cx } from '../../lib/cx'
import { fmtBytes, fmtCompact, fmtDateTime, fmtMs, fmtNumber, fmtPct, fmtSeconds, fmtUsd, humanize } from '../../lib/format'
import { BarList, ColumnChart, Meter, PercentileTable } from '../charts'
import { Figures, MetricCard, SubTitle } from './MetricCard'

const percentileRows = (record: Record<string, Percentiles> | undefined) => Object.entries(record ?? {}).map(([k, p]) => [humanize(k), p] as [string, Percentiles])
const msOrDash = (n: number | null) => fmtMs(n)

// ---- GET /metrics/documents ------------------------------------------------------------------------------------
export function DocumentsCard({ state }: { state: ApiState<DocumentStats> }) {
  return (
    <MetricCard title="Documents" description="Library composition (all time)" state={state}>
      {(d) => (
        <div className="space-y-6">
          <Figures
            items={[
              ['Documents', fmtNumber(d.totals.documents)],
              ['Pages', fmtCompact(d.totals.pages)],
              ['Chunks', fmtCompact(d.totals.chunks)],
              ['Tokens', fmtCompact(d.totals.tokens)],
              ['Stored', fmtBytes(d.totals.bytes)],
              ['Dedup hits (free re-uploads)', fmtNumber(d.totals.dedup_hits)],
            ]}
          />
          <div className="grid gap-6 sm:grid-cols-2">
            <div>
              <SubTitle>By status</SubTitle>
              <BarList data={recordToBars(d.by_status, humanize)} />
            </div>
            <div>
              <SubTitle>By category</SubTitle>
              <BarList data={recordToBars(d.by_category, humanize)} empty="No categorised documents yet" />
            </div>
            <div>
              <SubTitle>Top tags</SubTitle>
              <BarList data={d.top_tags.slice(0, 8).map((t) => ({ key: t.key, label: t.key, value: t.count }))} empty="No tags yet" />
            </div>
            <div>
              <SubTitle>Uploads per day (14 days)</SubTitle>
              <ColumnChart data={d.uploads_per_day.map((x) => ({ key: x.day, label: x.day.slice(5), value: x.count }))} />
            </div>
          </div>
          <p className="text-xs text-zinc-500">
            Time to ready: p50 {fmtSeconds(d.seconds_to_ready.p50)} · p95 {fmtSeconds(d.seconds_to_ready.p95)}
          </p>
        </div>
      )}
    </MetricCard>
  )
}

// ---- GET /metrics/processing?window=24h ------------------------------------------------------------------------
export function ProcessingCard({ state }: { state: ApiState<ProcessingMetrics> }) {
  return (
    <MetricCard title="Processing" description="Ingest and insights attempts, last 24 h" state={state}>
      {(p) => (
        <div className="space-y-6">
          <Figures
            items={[
              ['Success rate', fmtPct(p.success_rate, 1)],
              ['Attempts', fmtNumber(p.attempts)],
              ['Failed', fmtNumber(p.failed)],
              ['Retried', fmtNumber(p.retried)],
              ['Crashed (worker lost)', fmtNumber(p.crashed)],
              ['In flight / stale', `${fmtNumber(p.in_flight)} / ${fmtNumber(p.stale)}`],
            ]}
          />
          <div className="grid gap-6 sm:grid-cols-2">
            <div>
              <SubTitle>Stage duration</SubTitle>
              <PercentileTable rows={percentileRows(p.stage_ms)} format={msOrDash} />
            </div>
            <div>
              <SubTitle>Task duration</SubTitle>
              <PercentileTable rows={percentileRows(p.duration_ms)} format={msOrDash} keyLabel="Task" />
            </div>
            <div>
              <SubTitle>Failures by code</SubTitle>
              <BarList data={p.failures_by_code.map((f) => ({ key: f.key, label: <span className="font-mono">{f.key}</span>, value: f.count }))} empty="No failures" />
            </div>
            <div>
              <SubTitle>Queue depth</SubTitle>
              <ul className="space-y-1.5 text-sm">
                {Object.entries(p.queue_depth).map(([queue, depth]) => (
                  <li key={queue} className="flex justify-between">
                    <span className="font-mono text-zinc-600">{queue}</span>
                    <span className="text-zinc-900 tabular-nums">{depth == null ? 'Redis unreachable' : `${fmtNumber(depth)} waiting`}</span>
                  </li>
                ))}
              </ul>
              <div className="mt-4">
                <SubTitle>Attempts per hour</SubTitle>
                <ColumnChart height={56} data={p.throughput.map((h) => ({ key: h.hour, label: h.hour.slice(11, 16), value: h.succeeded + h.failed }))} />
              </div>
            </div>
          </div>
          {p.recent_failures.length > 0 && (
            <div>
              <SubTitle>Recent failures</SubTitle>
              <div className="overflow-x-auto">
                <table className="w-full min-w-[560px] text-xs">
                  <tbody className="divide-y divide-zinc-100">
                    {p.recent_failures.slice(0, 6).map((f, i) => (
                      <tr key={i}>
                        <td className="py-1.5 pr-3 whitespace-nowrap text-zinc-500">{fmtDateTime(f.at)}</td>
                        <td className="max-w-48 truncate py-1.5 pr-3 text-zinc-800">{f.filename ?? '(deleted)'}</td>
                        <td className="py-1.5 pr-3 text-zinc-500">{f.task}</td>
                        <td className="py-1.5 pr-3 font-mono text-red-700">{f.error_code ?? '—'}</td>
                        <td className="max-w-64 truncate py-1.5 text-zinc-500" title={f.error_message ?? undefined}>
                          {f.error_message}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </div>
      )}
    </MetricCard>
  )
}

// ---- GET /metrics/ai-usage?window=7d&group_by=... --------------------------------------------------------------
const GROUPS: UsageGroupBy[] = ['operation', 'model', 'day']

export function AiUsageCard({ state, groupBy, onGroupBy }: { state: ApiState<AIUsageMetrics>; groupBy: UsageGroupBy; onGroupBy: (g: UsageGroupBy) => void }) {
  return (
    <MetricCard
      title="AI usage and cost"
      description="Every model call is recorded in the ai_usage ledger, last 7 days"
      state={state}
      actions={
        <div className="flex rounded-md border border-zinc-200 p-0.5" role="group" aria-label="Group cost by">
          {GROUPS.map((g) => (
            <button
              key={g}
              type="button"
              onClick={() => onGroupBy(g)}
              aria-pressed={groupBy === g}
              className={cx('rounded px-2 py-0.5 text-xs', groupBy === g ? 'bg-zinc-900 text-white' : 'text-zinc-600 hover:bg-zinc-100')}
            >
              {humanize(g)}
            </button>
          ))}
        </div>
      }
    >
      {(u) => (
        <div className="space-y-6">
          <Figures
            items={[
              ['Cost (7 d)', fmtUsd(u.totals.cost_usd)],
              ['Model calls', fmtNumber(u.totals.calls)],
              ['Tokens in / out', `${fmtCompact(u.totals.input_tokens)} / ${fmtCompact(u.totals.output_tokens)}`],
              ['Cache hit rate', fmtPct(u.totals.cache_hit_rate)],
              ['Cost per document', fmtUsd(u.cost_per_document_usd)],
              ['Cost per answer', fmtUsd(u.cost_per_answer_usd)],
            ]}
          />
          <div>
            <SubTitle>Cost by {u.group_by}</SubTitle>
            {u.group_by === 'day' ? (
              <ColumnChart format={fmtUsd} data={[...u.groups].sort((a, b) => a.key.localeCompare(b.key)).map((g) => ({ key: g.key, label: g.key.slice(5), value: g.cost_usd }))} />
            ) : (
              <BarList
                format={fmtUsd}
                data={[...u.groups]
                  .sort((a, b) => b.cost_usd - a.cost_usd)
                  .map((g) => ({ key: g.key, label: <span className="font-mono">{g.key}</span>, value: g.cost_usd, hint: `${fmtNumber(g.calls)} calls · ${fmtMs(g.avg_latency_ms)} avg` }))}
                empty="No model calls in this window"
              />
            )}
          </div>
          <div className="grid gap-x-4 gap-y-1 text-xs text-zinc-500 sm:grid-cols-3">
            <span>Cached input: {fmtCompact(u.totals.cached_input_tokens)} tokens</span>
            <span>Estimated usage (interrupted streams): {fmtPct(u.totals.estimated_share, 1)}</span>
            <span>Call error rate: {fmtPct(u.totals.error_rate, 1)}</span>
          </div>
        </div>
      )}
    </MetricCard>
  )
}

/** Today's spend against the per-user soft daily limit. */
export function SpendMeter({ usage }: { usage: AIUsageMetrics }) {
  return (
    <div>
      <p className="mt-1 text-2xl font-semibold tracking-tight text-zinc-900">{fmtUsd(usage.today_spend_usd)}</p>
      <div className="mt-2">
        <Meter value={usage.today_spend_usd} max={usage.daily_limit_usd} label="Today's spend against the daily limit" />
      </div>
      <p className="mt-1 text-xs text-zinc-500">
        of {fmtUsd(usage.daily_limit_usd)} daily limit ({fmtPct(usage.daily_limit_usd > 0 ? usage.today_spend_usd / usage.daily_limit_usd : null)})
      </p>
    </div>
  )
}

// ---- GET /metrics/chat?window=7d -------------------------------------------------------------------------------
export function ChatCard({ state }: { state: ApiState<ChatMetrics> }) {
  return (
    <MetricCard title="Chat quality and latency" description="Assistant answers, last 7 days" state={state}>
      {(c) => (
        <div className="space-y-6">
          <Figures
            items={[
              ['Answers', fmtNumber(c.answers)],
              ['First token p50 / p95', `${fmtMs(c.ttft_ms.p50)} / ${fmtMs(c.ttft_ms.p95)}`],
              ['Total p50 / p95', `${fmtMs(c.latency_ms.p50)} / ${fmtMs(c.latency_ms.p95)}`],
              ['Citations per answer', c.avg_citations == null ? '—' : c.avg_citations.toFixed(1)],
              ['Invalid citations', fmtNumber(c.invalid_citations)],
              ['Uncited claims', fmtNumber(c.uncited_claims)],
            ]}
          />
          <div className="grid gap-6 sm:grid-cols-3">
            <div>
              <SubTitle>Answer types</SubTitle>
              <BarList data={recordToBars(c.answer_types, humanize)} />
            </div>
            <div>
              <SubTitle>Confidence</SubTitle>
              <BarList data={recordToBars(c.confidence, humanize)} />
            </div>
            <div>
              <SubTitle>Not found: by path</SubTitle>
              <BarList
                data={recordToBars(c.not_found_by_path, (k) => (k === 'gate' ? 'Gate (no model call)' : k === 'model' ? 'Model refused' : humanize(k)))}
                empty="No refusals"
              />
            </div>
          </div>
          <div className="grid gap-6 sm:grid-cols-2">
            <div>
              <SubTitle>Pipeline steps</SubTitle>
              <PercentileTable rows={percentileRows(c.stage_ms)} format={msOrDash} />
            </div>
            <div className="text-sm">
              <SubTitle>Reliability</SubTitle>
              <ul className="space-y-1.5">
                <ReliabilityRow label="Served by the fallback model" value={c.fallback_model} />
                <ReliabilityRow label="Failed mid-stream" value={c.errors} />
                <ReliabilityRow label="Interrupted (client left)" value={c.interrupted} />
              </ul>
            </div>
          </div>
        </div>
      )}
    </MetricCard>
  )
}

function ReliabilityRow({ label, value }: { label: string; value: number }) {
  return (
    <li className="flex justify-between">
      <span className="text-zinc-600">{label}</span>
      <span className="text-zinc-900 tabular-nums">{fmtNumber(value)}</span>
    </li>
  )
}

// ---- GET /metrics/system?window=1h -----------------------------------------------------------------------------
export function SystemCard({ state }: { state: ApiState<SystemMetrics> }) {
  return (
    <MetricCard title="API routes" description="Request logs, last hour (all users)" state={state}>
      {(s) =>
        s.routes.length === 0 ? (
          <p className="py-4 text-center text-sm text-zinc-400">No requests logged in this window.</p>
        ) : (
          <div className="overflow-x-auto">
            <p className="mb-3 text-xs text-zinc-500">{fmtNumber(s.requests)} requests</p>
            <table className="w-full min-w-[640px] text-sm tabular-nums">
              <thead>
                <tr className="text-left text-xs text-zinc-500">
                  <th className="pb-1.5 font-medium">Route</th>
                  <th className="pb-1.5 text-right font-medium">Requests</th>
                  <th className="pb-1.5 text-right font-medium">TTFB p50</th>
                  <th className="pb-1.5 text-right font-medium">TTFB p95</th>
                  <th className="pb-1.5 text-right font-medium">4xx</th>
                  <th className="pb-1.5 text-right font-medium">5xx</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-zinc-100">
                {[...s.routes]
                  .sort((a, b) => b.count - a.count)
                  .map((r) => (
                    <tr key={`${r.method} ${r.route}`}>
                      <td className="py-1.5 pr-3">
                        <span className="mr-2 inline-block w-14 font-mono text-xs text-zinc-500">{r.method}</span>
                        <span className="font-mono text-xs text-zinc-800">{r.route}</span>
                      </td>
                      <td className="py-1.5 text-right">{fmtNumber(r.count)}</td>
                      <td className="py-1.5 text-right">{fmtMs(r.ttfb_ms.p50)}</td>
                      <td className="py-1.5 text-right">{fmtMs(r.ttfb_ms.p95)}</td>
                      <td className={cx('py-1.5 text-right', r.error_4xx_rate > 0 ? 'text-amber-700' : 'text-zinc-400')}>{fmtPct(r.error_4xx_rate, 1)}</td>
                      <td className={cx('py-1.5 text-right', r.error_5xx_rate > 0 ? 'font-medium text-red-700' : 'text-zinc-400')}>{fmtPct(r.error_5xx_rate, 1)}</td>
                    </tr>
                  ))}
              </tbody>
            </table>
          </div>
        )
      }
    </MetricCard>
  )
}
