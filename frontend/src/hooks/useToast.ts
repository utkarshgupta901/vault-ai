import { createContext, useContext } from 'react'
import { ApiError } from '../api/client'

export type ToastKind = 'success' | 'info' | 'warning' | 'error'

export interface ToastInput {
  kind: ToastKind
  title: string
  message?: string
  /** Shown for errors so a failure can be traced in the API logs (X-Request-ID). */
  requestId?: string | null
}

export interface ToastApi {
  show: (toast: ToastInput) => void
  /** Shows any thrown error; ApiErrors display their code, message and request id. */
  error: (err: unknown, title?: string) => void
}

export const ToastContext = createContext<ToastApi | null>(null)

export function useToast(): ToastApi {
  const ctx = useContext(ToastContext)
  if (!ctx) throw new Error('useToast must be used inside <ToastProvider>')
  return ctx
}

export function errorToToast(err: unknown, title?: string): ToastInput {
  if (err instanceof ApiError) {
    return { kind: 'error', title: title ?? err.code, message: title ? `${err.code}: ${err.message}` : err.message, requestId: err.requestId }
  }
  return { kind: 'error', title: title ?? 'Something went wrong', message: err instanceof Error ? err.message : String(err) }
}
