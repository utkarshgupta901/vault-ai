import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { isAbortError } from '../api/client'

export interface ApiState<T> {
  data: T | undefined
  error: unknown
  loading: boolean
  /** Refetch with the same key. Previous data stays on screen meanwhile (no skeleton flash on refresh). */
  reload: () => void
  setData: (update: (prev: T | undefined) => T | undefined) => void
}

/**
 * Minimal data-fetching hook. `key` identifies *what* is loaded: when it changes, the old request is aborted and the
 * data resets; `null` means "nothing to load yet". The fetcher is kept in a ref so callers can pass an inline closure
 * without re-triggering the effect on every render.
 */
export function useApi<T>(key: string | null, fetcher: (signal: AbortSignal) => Promise<T>): ApiState<T> {
  const fetcherRef = useLatest(fetcher)
  const [state, setState] = useState<{ key: string | null; data?: T; error?: unknown; loading: boolean }>({
    key,
    loading: key !== null,
  })
  const [nonce, setNonce] = useState(0)

  // A new key means different data: drop what we had so the UI shows a loading state, not the previous item.
  if (state.key !== key) setState({ key, loading: key !== null })

  useEffect(() => {
    if (key === null) return
    const controller = new AbortController()
    fetcherRef
      .current(controller.signal)
      .then((data) => setState({ key, data, loading: false }))
      .catch((error: unknown) => {
        if (!isAbortError(error)) setState((s) => ({ ...s, error, loading: false }))
      })
    return () => controller.abort()
  }, [key, nonce, fetcherRef])

  const reload = useCallback(() => {
    setState((s) => ({ ...s, loading: true }))
    setNonce((n) => n + 1)
  }, [])
  const setData = useCallback(
    (update: (prev: T | undefined) => T | undefined) => setState((s) => ({ ...s, data: update(s.data) })),
    [],
  )
  return { data: state.data, error: state.error, loading: state.loading, reload, setData }
}

/** Calls `tick` every `ms` while the tab is visible (no point refreshing a dashboard nobody is looking at). */
export function useInterval(tick: () => void, ms: number): void {
  const tickRef = useLatest(tick)
  useEffect(() => {
    const id = setInterval(() => {
      if (document.visibilityState === 'visible') tickRef.current()
    }, ms)
    return () => clearInterval(id)
  }, [ms, tickRef])
}

export function useDebounced<T>(value: T, ms = 300): T {
  const [debounced, setDebounced] = useState(value)
  useEffect(() => {
    const id = setTimeout(() => setDebounced(value), ms)
    return () => clearTimeout(id)
  }, [value, ms])
  return debounced
}

/** A ref that always holds the latest value, for callbacks used inside effects that should not re-run. */
export function useLatest<T>(value: T) {
  const ref = useRef(value)
  useLayoutEffect(() => {
    ref.current = value
  })
  return ref
}
