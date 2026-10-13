import type { ReactNode } from 'react'
import type { ApiState } from '../../hooks/useApi'
import { cx } from '../../lib/cx'
import { Card, CardHeader, ErrorState, SkeletonRows } from '../ui'

interface MetricCardProps<T> {
  title: string
  description?: string
  state: ApiState<T>
  actions?: ReactNode
  children: (data: T) => ReactNode
  className?: string
}

/**
 * A card bound to one metrics endpoint. It owns the three non-data states: first load (skeleton), failure (error with
 * request id, or "not available yet" for a 404 route), and refresh (the previous numbers stay, slightly dimmed, so
 * the 10-second auto-refresh never makes the layout jump).
 */
export function MetricCard<T>({ title, description, state, actions, children, className }: MetricCardProps<T>) {
  const { data, error, loading, reload } = state
  return (
    <Card className={className}>
      <CardHeader title={title} description={description} actions={actions} />
      {data !== undefined ? (
        <div className={cx('p-4 transition-opacity', loading && 'opacity-70')}>{children(data)}</div>
      ) : error ? (
        <ErrorState error={error} onRetry={reload} what={title.toLowerCase()} />
      ) : (
        <SkeletonRows rows={4} />
      )}
    </Card>
  )
}

/** Compact label/value grid used inside metric cards. */
export function Figures({ items }: { items: [string, ReactNode][] }) {
  return (
    <dl className="grid grid-cols-2 gap-x-4 gap-y-3 sm:grid-cols-3">
      {items.map(([label, value]) => (
        <div key={label} className="min-w-0">
          <dt className="truncate text-xs text-zinc-500">{label}</dt>
          <dd className="mt-0.5 text-base font-semibold text-zinc-900">{value}</dd>
        </div>
      ))}
    </dl>
  )
}

export function SubTitle({ children }: { children: ReactNode }) {
  return <h3 className="mb-2 text-xs font-semibold tracking-wide text-zinc-500 uppercase">{children}</h3>
}
