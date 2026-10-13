/** Small presentational primitives shared by every page. No behaviour beyond what the markup needs. */
import { AlertTriangle, Loader2, RotateCw, type LucideIcon } from 'lucide-react'
import type { ButtonHTMLAttributes, ReactNode } from 'react'
import { ApiError, isRouteMissing } from '../api/client'
import { cx } from '../lib/cx'

type ButtonVariant = 'primary' | 'secondary' | 'ghost' | 'danger'

const BUTTON_VARIANTS: Record<ButtonVariant, string> = {
  primary: 'bg-accent-600 text-white hover:bg-accent-700 disabled:bg-accent-600/50',
  secondary: 'border border-zinc-300 bg-white text-zinc-800 hover:bg-zinc-50 disabled:text-zinc-400',
  ghost: 'text-zinc-600 hover:bg-zinc-100 hover:text-zinc-900 disabled:text-zinc-300',
  danger: 'bg-red-600 text-white hover:bg-red-700 disabled:bg-red-600/50',
}

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant
  size?: 'sm' | 'md'
  icon?: LucideIcon
  busy?: boolean
}

export function Button({ variant = 'secondary', size = 'md', icon: Icon, busy, className, children, disabled, ...rest }: ButtonProps) {
  return (
    <button
      type="button"
      disabled={disabled || busy}
      className={cx(
        'inline-flex shrink-0 items-center justify-center gap-1.5 rounded-md font-medium transition-colors disabled:cursor-not-allowed',
        size === 'sm' ? 'h-7 px-2 text-xs' : 'h-9 px-3 text-sm',
        BUTTON_VARIANTS[variant],
        className,
      )}
      {...rest}
    >
      {busy ? <Loader2 className="size-4 animate-spin" aria-hidden /> : Icon && <Icon className="size-4" aria-hidden />}
      {children}
    </button>
  )
}

export type Tone = 'neutral' | 'blue' | 'green' | 'amber' | 'red' | 'violet'

const TONES: Record<Tone, string> = {
  neutral: 'bg-zinc-100 text-zinc-700 ring-zinc-200',
  blue: 'bg-accent-50 text-accent-700 ring-accent-100',
  green: 'bg-emerald-50 text-emerald-700 ring-emerald-200',
  amber: 'bg-amber-50 text-amber-800 ring-amber-200',
  red: 'bg-red-50 text-red-700 ring-red-200',
  violet: 'bg-violet-50 text-violet-700 ring-violet-200',
}

export function Badge({ tone = 'neutral', icon: Icon, title, children, className }: { tone?: Tone; icon?: LucideIcon; title?: string; children: ReactNode; className?: string }) {
  return (
    <span
      title={title}
      className={cx('inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium whitespace-nowrap ring-1 ring-inset', TONES[tone], className)}
    >
      {Icon && <Icon className="size-3.5" aria-hidden />}
      {children}
    </span>
  )
}

export function Card({ children, className }: { children: ReactNode; className?: string }) {
  return <section className={cx('rounded-lg border border-zinc-200 bg-white shadow-xs', className)}>{children}</section>
}

export function CardHeader({ title, description, actions }: { title: ReactNode; description?: ReactNode; actions?: ReactNode }) {
  return (
    <div className="flex flex-wrap items-start justify-between gap-3 border-b border-zinc-100 px-4 py-3">
      <div className="min-w-0">
        <h2 className="text-sm font-semibold text-zinc-900">{title}</h2>
        {description && <p className="mt-0.5 text-xs text-zinc-500">{description}</p>}
      </div>
      {actions && <div className="flex items-center gap-2">{actions}</div>}
    </div>
  )
}

export function Skeleton({ className }: { className?: string }) {
  return <div className={cx('animate-pulse rounded bg-zinc-200/70', className)} aria-hidden />
}

export function SkeletonRows({ rows = 4 }: { rows?: number }) {
  return (
    <div className="space-y-3 p-4" role="status" aria-label="Loading">
      {Array.from({ length: rows }, (_, i) => (
        <Skeleton key={i} className="h-5 w-full" />
      ))}
    </div>
  )
}

export function Spinner({ className }: { className?: string }) {
  return <Loader2 className={cx('size-4 animate-spin text-zinc-400', className)} aria-label="Loading" />
}

export function EmptyState({ icon: Icon, title, children, action }: { icon: LucideIcon; title: string; children?: ReactNode; action?: ReactNode }) {
  return (
    <div className="flex flex-col items-center justify-center px-6 py-10 text-center">
      <div className="mb-3 rounded-full bg-zinc-100 p-3">
        <Icon className="size-5 text-zinc-500" aria-hidden />
      </div>
      <p className="text-sm font-medium text-zinc-900">{title}</p>
      {children && <div className="mt-1 max-w-md text-sm text-zinc-500">{children}</div>}
      {action && <div className="mt-4">{action}</div>}
    </div>
  )
}

/** Error panel for a failed load: shows the envelope's code, message and request id, plus a retry button. A route
 *  that does not exist yet (work package not merged) gets a calm "not available" message instead. */
export function ErrorState({ error, onRetry, what = 'data' }: { error: unknown; onRetry?: () => void; what?: string }) {
  if (isRouteMissing(error)) {
    return (
      <EmptyState icon={AlertTriangle} title="Not available on this server yet">
        The API does not expose this {what} endpoint. It appears once that feature is deployed.
      </EmptyState>
    )
  }
  const apiError = error instanceof ApiError ? error : null
  return (
    <div className="flex flex-col items-center px-6 py-8 text-center" role="alert">
      <AlertTriangle className="mb-2 size-5 text-red-500" aria-hidden />
      <p className="text-sm font-medium text-zinc-900">
        Could not load {what}
        {apiError && <span className="ml-1 font-mono text-xs text-red-600">{apiError.code}</span>}
      </p>
      <p className="mt-1 max-w-md text-sm text-zinc-500">{error instanceof Error ? error.message : String(error)}</p>
      {apiError?.requestId && <p className="mt-1 font-mono text-xs text-zinc-400">request {apiError.requestId}</p>}
      {onRetry && (
        <Button className="mt-4" size="sm" icon={RotateCw} onClick={onRetry}>
          Retry
        </Button>
      )}
    </div>
  )
}

export function ProgressBar({ value, className }: { value: number; className?: string }) {
  const pct = Math.max(0, Math.min(100, value))
  return (
    <div className={cx('h-1.5 w-full overflow-hidden rounded-full bg-zinc-200', className)} role="progressbar" aria-valuenow={pct} aria-valuemin={0} aria-valuemax={100}>
      <div className="h-full rounded-full bg-accent-500 transition-[width] duration-300" style={{ width: `${pct}%` }} />
    </div>
  )
}

export function PageHeader({ title, description, actions }: { title: string; description?: ReactNode; actions?: ReactNode }) {
  return (
    <div className="mb-5 flex flex-wrap items-end justify-between gap-3">
      <div>
        <h1 className="text-xl font-semibold tracking-tight text-zinc-900">{title}</h1>
        {description && <p className="mt-1 text-sm text-zinc-500">{description}</p>}
      </div>
      {actions && <div className="flex items-center gap-2">{actions}</div>}
    </div>
  )
}

/** Label/value pair for metadata lists. */
export function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="min-w-0">
      <dt className="text-xs text-zinc-500">{label}</dt>
      <dd className="mt-0.5 truncate text-sm text-zinc-900">{children}</dd>
    </div>
  )
}
