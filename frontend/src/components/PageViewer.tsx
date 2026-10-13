import { ChevronLeft, ChevronRight, FileText } from 'lucide-react'
import { useEffect, useMemo, useRef } from 'react'
import { getDocument, getPage } from '../api/documents'
import { useApi } from '../hooks/useApi'
import { Dialog } from './Dialog'
import { Button, ErrorState, Skeleton } from './ui'

interface PageViewerProps {
  documentId: string
  page: number
  onPageChange: (page: number) => void
  /** Total pages if known; unknown disables nothing (the server answers 404 past the end). */
  pageCount?: number | null
  /** Text to highlight, e.g. a citation snippet or a risk-flag quote. */
  highlight?: string | null
}

/** Escapes regex metacharacters so arbitrary snippet text can be matched literally. */
function escapeRegExp(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

/** Splits page text around the first match of `highlight`, matching across any whitespace/line-break differences
 *  between the stored snippet and the page (PDF extraction often breaks lines mid-sentence). */
function splitHighlight(text: string, highlight?: string | null): [string, string, string] | null {
  const words = highlight?.trim().split(/\s+/).filter(Boolean)
  if (!words?.length) return null
  const match = new RegExp(words.map(escapeRegExp).join('\\s+'), 'i').exec(text)
  if (!match) return null
  return [text.slice(0, match.index), match[0], text.slice(match.index + match[0].length)]
}

/** Extracted text of one page (GET /documents/{id}/pages/{n}): what citations point at. */
export function PageViewer({ documentId, page, onPageChange, pageCount, highlight }: PageViewerProps) {
  const { data, error, loading, reload } = useApi(`page:${documentId}:${page}`, (signal) => getPage(documentId, page, signal))
  const parts = useMemo(() => (data ? splitHighlight(data.text, highlight) : null), [data, highlight])
  const markRef = useRef<HTMLElement>(null)

  useEffect(() => {
    markRef.current?.scrollIntoView({ block: 'center', behavior: 'smooth' })
  }, [parts])

  const atEnd = pageCount != null && page >= pageCount
  return (
    <div className="flex min-h-0 flex-col">
      <div className="flex items-center justify-between gap-2 pb-3">
        <div className="min-w-0 text-sm">
          <span className="font-medium text-zinc-900">{data?.label ?? `Page ${page}`}</span>
          {pageCount != null && <span className="text-zinc-500"> · page {page} of {pageCount}</span>}
          {data?.heading_path && <p className="truncate text-xs text-zinc-500">{data.heading_path}</p>}
        </div>
        <div className="flex gap-1">
          <Button size="sm" icon={ChevronLeft} disabled={page <= 1} onClick={() => onPageChange(page - 1)} aria-label="Previous page">
            Prev
          </Button>
          <Button size="sm" disabled={atEnd} onClick={() => onPageChange(page + 1)} aria-label="Next page">
            Next
            <ChevronRight className="size-4" aria-hidden />
          </Button>
        </div>
      </div>
      <div className="min-h-48 overflow-y-auto rounded-md border border-zinc-200 bg-zinc-50 p-4">
        {loading && !data ? (
          <div className="space-y-2">
            {Array.from({ length: 8 }, (_, i) => (
              <Skeleton key={i} className="h-4 w-full" />
            ))}
          </div>
        ) : error ? (
          <ErrorState error={error} onRetry={reload} what="this page" />
        ) : data ? (
          <p className="font-serif text-sm leading-relaxed whitespace-pre-wrap text-zinc-800">
            {parts ? (
              <>
                {parts[0]}
                <mark ref={markRef} className="rounded bg-amber-200/70 px-0.5">
                  {parts[1]}
                </mark>
                {parts[2]}
              </>
            ) : (
              data.text || <span className="text-zinc-400 italic">This page has no extractable text.</span>
            )}
          </p>
        ) : null}
      </div>
    </div>
  )
}

export interface PageTarget {
  documentId: string
  page: number
  highlight?: string | null
  filename?: string
}

/** The page viewer in a modal, opened from citation chips. Loads the document for its filename and page count. */
export function PageViewerModal({ target, onClose, onPageChange }: { target: PageTarget | null; onClose: () => void; onPageChange: (page: number) => void }) {
  const { data: doc } = useApi(target ? `doc:${target.documentId}` : null, (signal) => getDocument(target!.documentId, signal))
  return (
    <Dialog
      open={target !== null}
      onClose={onClose}
      className="w-[48rem]"
      title={
        <span className="flex items-center gap-2">
          <FileText className="size-4 text-zinc-400" aria-hidden />
          {doc?.filename ?? target?.filename ?? 'Document'}
        </span>
      }
    >
      {target && (
        <PageViewer documentId={target.documentId} page={target.page} pageCount={doc?.page_count} highlight={target.highlight} onPageChange={onPageChange} />
      )}
    </Dialog>
  )
}
