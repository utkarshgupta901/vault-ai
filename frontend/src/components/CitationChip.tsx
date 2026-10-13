import { ExternalLink, FileText, TriangleAlert } from 'lucide-react'
import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import type { CitationInfo } from '../lib/citations'
import { cx } from '../lib/cx'

const POPOVER_WIDTH = 320

/**
 * An inline [S1] chip. Clicking opens a popover with the source (filename, page label, snippet) and "Open page".
 * The popover is portalled to <body> with fixed positioning so the scrolling chat panel can never clip it.
 */
export function CitationChip({ info, onOpenPage }: { info: CitationInfo; onOpenPage?: (info: CitationInfo) => void }) {
  const [open, setOpen] = useState(false)
  const [pos, setPos] = useState<{ top: number; left: number } | null>(null)
  const chipRef = useRef<HTMLButtonElement>(null)
  const popRef = useRef<HTMLDivElement>(null)

  useLayoutEffect(() => {
    if (!open || !chipRef.current) return
    const r = chipRef.current.getBoundingClientRect()
    const left = Math.min(Math.max(8, r.left), window.innerWidth - POPOVER_WIDTH - 8)
    const below = r.bottom + 6
    // Flip above the chip when there is no room below.
    const height = popRef.current?.offsetHeight ?? 160
    const top = below + height > window.innerHeight - 8 ? Math.max(8, r.top - height - 6) : below
    setPos({ top, left })
  }, [open])

  useEffect(() => {
    if (!open) return
    const close = (e: Event) => {
      if (e.type === 'keydown' && (e as KeyboardEvent).key !== 'Escape') return
      if (e.type === 'mousedown' && (popRef.current?.contains(e.target as Node) || chipRef.current?.contains(e.target as Node))) return
      setOpen(false)
    }
    document.addEventListener('mousedown', close)
    document.addEventListener('keydown', close)
    window.addEventListener('resize', close)
    window.addEventListener('scroll', close, true) // capture: any scrolling container moves the chip
    return () => {
      document.removeEventListener('mousedown', close)
      document.removeEventListener('keydown', close)
      window.removeEventListener('resize', close)
      window.removeEventListener('scroll', close, true)
    }
  }, [open])

  const canOpenPage = info.valid && !info.sourceDeleted && info.documentId && info.pageNo != null && onOpenPage
  return (
    <>
      <button
        ref={chipRef}
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        aria-label={info.valid ? `Source ${info.ref}: ${info.filename} ${info.label}` : `Invalid citation ${info.ref}`}
        className={cx(
          'mx-0.5 inline-flex h-[1.15rem] items-center rounded px-1 align-[0.1em] font-mono text-[0.68rem] leading-none font-semibold ring-1 ring-inset transition-colors',
          !info.valid
            ? 'bg-red-50 text-red-600 line-through ring-red-200'
            : info.weakSupport
              ? 'bg-amber-50 text-amber-800 ring-amber-200 hover:bg-amber-100'
              : 'bg-accent-50 text-accent-700 ring-accent-100 hover:bg-accent-100',
        )}
      >
        {info.ref}
      </button>
      {open &&
        createPortal(
          <div
            ref={popRef}
            role="dialog"
            aria-label={`Source ${info.ref}`}
            style={{ top: pos?.top ?? -9999, left: pos?.left ?? -9999, width: POPOVER_WIDTH }}
            className="fixed z-50 rounded-lg border border-zinc-200 bg-white p-3 text-left shadow-xl"
          >
            {info.valid ? (
              <>
                <div className="flex items-start gap-2">
                  <FileText className="mt-0.5 size-4 shrink-0 text-zinc-400" aria-hidden />
                  <div className="min-w-0">
                    <p className="truncate text-sm font-medium text-zinc-900" title={info.filename}>
                      {info.filename}
                    </p>
                    <p className="text-xs text-zinc-500">
                      {info.ref} · {info.label}
                    </p>
                  </div>
                </div>
                {info.sourceDeleted ? (
                  <p className="mt-2 text-xs text-zinc-500 italic">The source document was deleted; its text is no longer shown.</p>
                ) : info.snippet ? (
                  <blockquote className="mt-2 border-l-2 border-zinc-200 pl-2 text-xs leading-relaxed text-zinc-600">{info.snippet}</blockquote>
                ) : null}
                {info.weakSupport && (
                  <p className="mt-2 flex items-center gap-1 text-xs text-amber-700">
                    <TriangleAlert className="size-3.5" aria-hidden /> Weak support: the cited text only partly backs this claim.
                  </p>
                )}
                {canOpenPage && (
                  <button
                    type="button"
                    onClick={() => {
                      setOpen(false)
                      onOpenPage(info)
                    }}
                    className="mt-3 inline-flex items-center gap-1 text-xs font-medium text-accent-600 hover:text-accent-700"
                  >
                    Open page <ExternalLink className="size-3" aria-hidden />
                  </button>
                )}
              </>
            ) : (
              <p className="text-xs text-zinc-600">
                <span className="font-medium text-red-600">Invalid citation.</span> The model cited {info.ref}, which was not one of the sources it was given, so it
                was discarded.
              </p>
            )}
          </div>,
          document.body,
        )}
    </>
  )
}
