import type { ReactNode } from 'react'

export interface BarDatum {
  key: string
  label: ReactNode
  value: number
  /** Optional secondary text shown after the value, e.g. "12 calls". */
  hint?: string
}

/** Turns {key: count} into bar data, largest first, dropping zeros. */
export function recordToBars(record: Record<string, number> | undefined, label: (key: string) => ReactNode = (k) => k): BarDatum[] {
  return Object.entries(record ?? {})
    .filter(([, v]) => v > 0)
    .sort((a, b) => b[1] - a[1])
    .map(([key, value]) => ({ key, label: label(key), value }))
}
