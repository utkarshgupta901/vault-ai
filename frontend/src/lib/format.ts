/** Display formatting. Everything returns a string and tolerates null/undefined, rendered as an em dash. */
import type { Money } from '../api/types'

const DASH = '—'
const numberFmt = new Intl.NumberFormat('en-US')
const compactFmt = new Intl.NumberFormat('en-US', { notation: 'compact', maximumFractionDigits: 1 })
const dateTimeFmt = new Intl.DateTimeFormat('en-US', { dateStyle: 'medium', timeStyle: 'short' })
const dateFmt = new Intl.DateTimeFormat('en-US', { dateStyle: 'medium' })
const utcDayFmt = new Intl.DateTimeFormat('en-US', { dateStyle: 'medium', timeZone: 'UTC' })

export function fmtNumber(n: number | null | undefined): string {
  return n == null ? DASH : numberFmt.format(n)
}

export function fmtCompact(n: number | null | undefined): string {
  return n == null ? DASH : compactFmt.format(n)
}

export function fmtBytes(n: number | null | undefined): string {
  if (n == null) return DASH
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
  return `${(n / 1024 / 1024).toFixed(1)} MB`
}

/** Costs here are often fractions of a cent, so small amounts keep more decimals. Accepts Decimal-as-string. */
export function fmtUsd(value: Money | null | undefined): string {
  if (value == null || value === '') return DASH
  const n = Number(value)
  if (!Number.isFinite(n)) return DASH
  if (n === 0) return '$0'
  if (Math.abs(n) < 0.01) return `$${n.toFixed(4)}`
  if (Math.abs(n) < 1) return `$${n.toFixed(3)}`
  return `$${n.toFixed(2)}`
}

export function fmtMs(ms: number | null | undefined): string {
  if (ms == null) return DASH
  if (ms < 1000) return `${Math.round(ms)} ms`
  return `${(ms / 1000).toFixed(ms < 10_000 ? 2 : 1)} s`
}

export function fmtSeconds(s: number | null | undefined): string {
  return s == null ? DASH : fmtMs(s * 1000)
}

/** Ratios arrive as 0..1. */
export function fmtPct(ratio: number | null | undefined, digits = 0): string {
  return ratio == null ? DASH : `${(ratio * 100).toFixed(digits)}%`
}

export function fmtScore(n: number | null | undefined, digits = 3): string {
  return n == null ? DASH : n.toFixed(digits)
}

export function fmtDateTime(iso: string | null | undefined): string {
  return iso ? dateTimeFmt.format(new Date(iso)) : DASH
}

export function fmtDate(iso: string | null | undefined): string {
  // A bare YYYY-MM-DD is a calendar day, not an instant: format it in UTC so it never shifts to the previous day.
  if (!iso) return DASH
  return iso.length === 10 ? utcDayFmt.format(new Date(`${iso}T00:00:00Z`)) : dateFmt.format(new Date(iso))
}

export function fmtRelative(iso: string | null | undefined, now = Date.now()): string {
  if (!iso) return DASH
  const seconds = Math.round((now - new Date(iso).getTime()) / 1000)
  if (seconds < 45) return 'just now'
  const minutes = Math.round(seconds / 60)
  if (minutes < 60) return `${minutes} min ago`
  const hours = Math.round(minutes / 60)
  if (hours < 24) return `${hours} h ago`
  return fmtDate(iso)
}

/** "financial_report" -> "Financial report" */
export function humanize(key: string | null | undefined): string {
  if (!key) return DASH
  const s = key.replace(/[_-]+/g, ' ').trim()
  return s.charAt(0).toUpperCase() + s.slice(1)
}

export function pluralize(n: number, one: string, many = `${one}s`): string {
  return `${fmtNumber(n)} ${n === 1 ? one : many}`
}
