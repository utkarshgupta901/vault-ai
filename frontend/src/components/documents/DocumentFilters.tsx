import { Search, X } from 'lucide-react'
import type { DocumentFilters as Filters } from '../../api/documents'
import { CATEGORIES, type DocumentStatus } from '../../api/types'
import { humanize } from '../../lib/format'

const STATUSES: DocumentStatus[] = ['queued', 'processing', 'ready', 'failed']

const control = 'h-9 rounded-md border border-zinc-300 bg-white px-2.5 text-sm text-zinc-800 focus:border-accent-500 focus:outline-none'

/** Server-side filters for GET /documents (status, category, tag, q). The search box is debounced by the page. */
export function DocumentFilters({ value, onChange }: { value: Filters; onChange: (next: Filters) => void }) {
  const set = (patch: Partial<Filters>) => onChange({ ...value, ...patch })
  const active = Boolean(value.status || value.category || value.tag || value.q)
  return (
    <div className="flex flex-wrap items-center gap-2 border-b border-zinc-100 px-4 py-3">
      <div className="relative min-w-56 flex-1">
        <Search className="pointer-events-none absolute top-2.5 left-2.5 size-4 text-zinc-400" aria-hidden />
        <input
          type="search"
          value={value.q ?? ''}
          onChange={(e) => set({ q: e.target.value })}
          placeholder="Search filename or title"
          aria-label="Search documents"
          className={`${control} w-full pl-8`}
        />
      </div>
      <select aria-label="Filter by status" value={value.status ?? ''} onChange={(e) => set({ status: e.target.value as Filters['status'] })} className={control}>
        <option value="">All statuses</option>
        {STATUSES.map((s) => (
          <option key={s} value={s}>
            {humanize(s)}
          </option>
        ))}
      </select>
      <select aria-label="Filter by category" value={value.category ?? ''} onChange={(e) => set({ category: e.target.value })} className={control}>
        <option value="">All categories</option>
        {CATEGORIES.map((c) => (
          <option key={c} value={c}>
            {humanize(c)}
          </option>
        ))}
      </select>
      <input value={value.tag ?? ''} onChange={(e) => set({ tag: e.target.value.trim().toLowerCase() })} placeholder="Tag" aria-label="Filter by tag" className={`${control} w-32`} />
      {active && (
        <button type="button" onClick={() => onChange({})} className="flex h-9 items-center gap-1 rounded-md px-2 text-sm text-zinc-500 hover:bg-zinc-100 hover:text-zinc-800">
          <X className="size-4" aria-hidden /> Clear
        </button>
      )}
    </div>
  )
}
