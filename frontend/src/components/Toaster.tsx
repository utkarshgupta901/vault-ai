import { AlertCircle, CheckCircle2, Info, TriangleAlert, X } from 'lucide-react'
import { useCallback, useMemo, useRef, useState, type ReactNode } from 'react'
import { ToastContext, errorToToast, type ToastApi, type ToastInput, type ToastKind } from '../hooks/useToast'
import { cx } from '../lib/cx'

interface Toast extends ToastInput {
  id: number
}

const ICONS: Record<ToastKind, typeof Info> = {
  success: CheckCircle2,
  info: Info,
  warning: TriangleAlert,
  error: AlertCircle,
}

const ICON_COLORS: Record<ToastKind, string> = {
  success: 'text-emerald-600',
  info: 'text-accent-600',
  warning: 'text-amber-600',
  error: 'text-red-600',
}

/** Errors stay longer: the request id is there to be copied. */
const DURATION_MS: Record<ToastKind, number> = { success: 4000, info: 5000, warning: 7000, error: 10000 }

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([])
  const nextId = useRef(1)

  const dismiss = useCallback((id: number) => setToasts((t) => t.filter((x) => x.id !== id)), [])

  const show = useCallback(
    (input: ToastInput) => {
      const id = nextId.current++
      setToasts((t) => [...t.slice(-4), { ...input, id }]) // keep the stack short
      setTimeout(() => dismiss(id), DURATION_MS[input.kind])
    },
    [dismiss],
  )

  // Memoised so components can list the toast api as an effect/callback dependency without re-running.
  const api = useMemo<ToastApi>(() => ({ show, error: (err, title) => show(errorToToast(err, title)) }), [show])

  return (
    <ToastContext.Provider value={api}>
      {children}
      <div className="pointer-events-none fixed right-4 bottom-4 z-50 flex w-96 max-w-[calc(100vw-2rem)] flex-col gap-2" aria-live="polite">
        {toasts.map((t) => {
          const Icon = ICONS[t.kind]
          return (
            <div key={t.id} role={t.kind === 'error' ? 'alert' : 'status'} className="pointer-events-auto flex gap-3 rounded-lg border border-zinc-200 bg-white p-3 shadow-lg">
              <Icon className={cx('mt-0.5 size-4 shrink-0', ICON_COLORS[t.kind])} aria-hidden />
              <div className="min-w-0 flex-1">
                <p className="text-sm font-medium break-words text-zinc-900">{t.title}</p>
                {t.message && <p className="mt-0.5 text-sm break-words text-zinc-600">{t.message}</p>}
                {t.requestId && <p className="mt-1 font-mono text-xs text-zinc-400 select-all">request {t.requestId}</p>}
              </div>
              <button type="button" onClick={() => dismiss(t.id)} className="self-start rounded p-0.5 text-zinc-400 hover:text-zinc-700" aria-label="Dismiss notification">
                <X className="size-4" />
              </button>
            </div>
          )
        })}
      </div>
    </ToastContext.Provider>
  )
}
