/**
 * Tiny chart primitives (HTML/CSS, no chart library). The dashboard only needs "compare a few magnitudes" and "a
 * count per day", so each chart is one series in one colour, the exact value is always printed next to its mark
 * (colour never carries meaning alone), and hovering a mark shows a native tooltip.
 */
import type { ReactNode } from 'react'
import type { Percentiles } from '../api/types'
import type { BarDatum } from '../lib/bars'
import { cx } from '../lib/cx'
import { fmtNumber } from '../lib/format'

/** Horizontal bars, sorted by the caller. Bars are thin (8px) with a rounded data end, labels in text colours. */
export function BarList({ data, format = fmtNumber, empty = 'No data yet' }: { data: BarDatum[]; format?: (n: number) => string; empty?: string }) {
  if (!data.length) return <p className="py-4 text-center text-sm text-zinc-400">{empty}</p>
  const max = Math.max(...data.map((d) => d.value), 0)
  return (
    <ul className="space-y-2.5">
      {data.map((d) => {
        const pct = max > 0 ? (d.value / max) * 100 : 0
        const valueText = format(d.value)
        return (
          <li key={d.key} title={`${typeof d.label === 'string' ? d.label : d.key}: ${valueText}${d.hint ? ` (${d.hint})` : ''}`}>
            <div className="mb-1 flex items-baseline justify-between gap-3 text-xs">
              <span className="min-w-0 truncate text-zinc-700">{d.label}</span>
              <span className="shrink-0 text-zinc-900 tabular-nums">
                {valueText}
                {d.hint && <span className="ml-1 text-zinc-400">{d.hint}</span>}
              </span>
            </div>
            <div className="h-2 rounded-r bg-chart-track">
              <div className="h-full rounded-r bg-chart-1" style={{ width: `${Math.max(pct, d.value > 0 ? 1.5 : 0)}%` }} />
            </div>
          </li>
        )
      })}
    </ul>
  )
}

export interface ColumnDatum {
  key: string
  label: string
  value: number
}

/** Vertical columns for a short time series (e.g. per day). The tallest column is labelled; the rest via hover. */
export function ColumnChart({ data, format = fmtNumber, height = 96 }: { data: ColumnDatum[]; format?: (n: number) => string; height?: number }) {
  if (!data.length) return <p className="py-4 text-center text-sm text-zinc-400">No data yet</p>
  const max = Math.max(...data.map((d) => d.value), 0)
  const peak = data.findIndex((d) => d.value === max && max > 0)
  return (
    <div>
      <div className="flex items-end gap-0.5 border-b border-zinc-300" style={{ height: height + 18 }}>
        {data.map((d, i) => (
          <div key={d.key} className="group flex h-full min-w-0 flex-1 flex-col items-center justify-end" title={`${d.label}: ${format(d.value)}`}>
            {i === peak && <span className="mb-0.5 text-[0.65rem] text-zinc-600 tabular-nums">{format(d.value)}</span>}
            <div
              className="w-full max-w-6 rounded-t bg-chart-1 transition-opacity group-hover:opacity-80"
              style={{ height: max > 0 ? Math.max((d.value / max) * height, d.value > 0 ? 2 : 0) : 0 }}
            />
          </div>
        ))}
      </div>
      <div className="mt-1 flex justify-between text-[0.65rem] text-zinc-400 tabular-nums">
        <span>{data[0].label}</span>
        {data.length > 2 && <span>{data[Math.floor(data.length / 2)].label}</span>}
        <span>{data[data.length - 1].label}</span>
      </div>
    </div>
  )
}

/** A single headline number. */
export function StatTile({ label, value, sub, className }: { label: string; value: ReactNode; sub?: ReactNode; className?: string }) {
  return (
    <div className={cx('rounded-lg border border-zinc-200 bg-white px-4 py-3 shadow-xs', className)}>
      <p className="text-xs text-zinc-500">{label}</p>
      <p className="mt-1 text-2xl font-semibold tracking-tight text-zinc-900">{value}</p>
      {sub && <p className="mt-0.5 text-xs text-zinc-500">{sub}</p>}
    </div>
  )
}

/** Usage against a limit. The fill turns amber past 80% and red at the limit; the text states the numbers. */
export function Meter({ value, max, label }: { value: number; max: number; label: string }) {
  const ratio = max > 0 ? value / max : 0
  const fill = ratio >= 1 ? 'bg-red-500' : ratio >= 0.8 ? 'bg-amber-500' : 'bg-chart-1'
  return (
    <div>
      <div className="h-2 overflow-hidden rounded-full bg-chart-track" role="meter" aria-label={label} aria-valuenow={value} aria-valuemin={0} aria-valuemax={max}>
        <div className={cx('h-full rounded-full', fill)} style={{ width: `${Math.min(100, ratio * 100)}%` }} />
      </div>
    </div>
  )
}

/** p50 / p95 / avg per key as a compact table (two percentiles of one measure read better as numbers than bars). */
export function PercentileTable({ rows, format, keyLabel = 'Stage' }: { rows: [string, Percentiles][]; format: (n: number | null) => string; keyLabel?: string }) {
  if (!rows.length) return <p className="py-4 text-center text-sm text-zinc-400">No data yet</p>
  return (
    <table className="w-full text-sm">
      <thead>
        <tr className="text-left text-xs text-zinc-500">
          <th className="pb-1.5 font-medium">{keyLabel}</th>
          <th className="pb-1.5 text-right font-medium">p50</th>
          <th className="pb-1.5 text-right font-medium">p95</th>
          <th className="pb-1.5 text-right font-medium">avg</th>
        </tr>
      </thead>
      <tbody className="divide-y divide-zinc-100 tabular-nums">
        {rows.map(([key, p]) => (
          <tr key={key}>
            <td className="py-1.5 text-zinc-700">{key}</td>
            <td className="py-1.5 text-right text-zinc-900">{format(p.p50)}</td>
            <td className="py-1.5 text-right text-zinc-900">{format(p.p95)}</td>
            <td className="py-1.5 text-right text-zinc-500">{format(p.avg)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}
