import { getReadiness } from '../api/metrics'
import type { ReadyResponse } from '../api/types'
import { useApi, useInterval } from '../hooks/useApi'
import { cx } from '../lib/cx'
import { humanize } from '../lib/format'

const POLL_MS = 15_000

type Level = 'ok' | 'degraded' | 'unavailable' | 'unknown'

const DOT: Record<Level, string> = {
  ok: 'bg-emerald-500',
  degraded: 'bg-amber-500',
  unavailable: 'bg-red-500',
  unknown: 'bg-zinc-300',
}

const LABEL: Record<Level, string> = {
  ok: 'All systems operational',
  degraded: 'Degraded',
  unavailable: 'Unavailable',
  unknown: 'Checking…',
}

function failingChecks(health: ReadyResponse): string[] {
  return Object.entries(health.checks)
    .filter(([, check]) => !check.ok)
    .map(([name, check]) => (check.error ? `${humanize(name)}: ${check.error}` : humanize(name)))
}

/**
 * Readiness from GET /health/ready. "degraded" (amber) means soft dependencies such as Redis or a worker pool are
 * down but the API still serves; "unavailable" (red) means a hard dependency (DB, pgvector, schema) failed, or the
 * API itself cannot be reached.
 */
export function HealthIndicator() {
  const { data, loading, reload } = useApi<ReadyResponse | null>('health', getReadiness)
  useInterval(reload, POLL_MS)

  const level: Level = data === undefined && loading ? 'unknown' : data ? data.status : 'unavailable'
  const failing = data ? failingChecks(data) : ['API unreachable']
  const detail = level === 'ok' || level === 'unknown' ? LABEL[level] : `${LABEL[level]}: ${failing.join('; ')}`

  return (
    <div className="group relative">
      <button type="button" onClick={reload} className="flex items-center gap-2 rounded-md px-2 py-1 text-xs text-zinc-600 hover:bg-zinc-100" aria-label={`API health: ${detail}. Click to re-check.`}>
        <span className={cx('size-2.5 rounded-full', DOT[level], level === 'ok' && 'shadow-[0_0_0_3px] shadow-emerald-500/20')} />
        <span className="hidden sm:inline">{level === 'unknown' ? 'Health' : humanize(level)}</span>
      </button>
      <div role="tooltip" className="invisible absolute top-full right-0 z-40 mt-1 w-72 rounded-md border border-zinc-200 bg-white p-3 text-xs text-zinc-700 opacity-0 shadow-lg transition-opacity group-focus-within:visible group-focus-within:opacity-100 group-hover:visible group-hover:opacity-100">
        <p className="font-medium text-zinc-900">{LABEL[level]}</p>
        {data && (
          <ul className="mt-2 space-y-1">
            {Object.entries(data.checks).map(([name, check]) => (
              <li key={name} className="flex items-start justify-between gap-2">
                <span className="flex items-center gap-1.5">
                  <span className={cx('size-1.5 rounded-full', check.ok ? 'bg-emerald-500' : 'bg-amber-500')} />
                  {humanize(name)}
                </span>
                <span className={cx('text-right', check.ok ? 'text-zinc-400' : 'text-amber-700')}>
                  {check.ok ? (check.ms != null ? `${check.ms} ms` : 'ok') : (check.error ?? 'failing')}
                </span>
              </li>
            ))}
          </ul>
        )}
        {data === null && <p className="mt-1 text-zinc-500">The API did not respond.</p>}
        {data?.models && (
          <p className="mt-2 border-t border-zinc-100 pt-2 text-zinc-500">
            Models: {Object.entries(data.models).map(([role, model]) => `${role} ${model}`).join(' · ')}
          </p>
        )}
      </div>
    </div>
  )
}
