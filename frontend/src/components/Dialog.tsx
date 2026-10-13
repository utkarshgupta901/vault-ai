import { X } from 'lucide-react'
import { useEffect, useRef, type ReactNode } from 'react'
import { cx } from '../lib/cx'

interface DialogProps {
  open: boolean
  onClose: () => void
  title: ReactNode
  description?: ReactNode
  children: ReactNode
  footer?: ReactNode
  className?: string
}

/**
 * Modal built on the native <dialog> element: showModal() gives us the backdrop, focus trapping, Escape-to-close and
 * correct stacking for free, with no portal or focus-trap library.
 */
export function Dialog({ open, onClose, title, description, children, footer, className }: DialogProps) {
  const ref = useRef<HTMLDialogElement>(null)

  useEffect(() => {
    const dialog = ref.current
    if (!dialog) return
    if (open && !dialog.open) dialog.showModal()
    if (!open && dialog.open) dialog.close()
  }, [open])

  return (
    <dialog
      ref={ref}
      onClose={onClose}
      // A click whose target is the <dialog> itself landed on the backdrop (content clicks hit inner elements).
      onClick={(e) => e.target === e.currentTarget && onClose()}
      className={cx('m-auto max-h-[85vh] w-[32rem] max-w-[calc(100vw-2rem)] overflow-hidden rounded-xl bg-white p-0 shadow-2xl', className)}
    >
      {open && (
        <div className="flex max-h-[85vh] flex-col">
          <div className="flex items-start justify-between gap-4 border-b border-zinc-100 px-5 py-4">
            <div className="min-w-0">
              <h2 className="text-base font-semibold text-zinc-900">{title}</h2>
              {description && <p className="mt-0.5 text-sm text-zinc-500">{description}</p>}
            </div>
            <button type="button" onClick={onClose} className="rounded p-1 text-zinc-400 hover:bg-zinc-100 hover:text-zinc-700" aria-label="Close">
              <X className="size-4" />
            </button>
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">{children}</div>
          {footer && <div className="flex justify-end gap-2 border-t border-zinc-100 bg-zinc-50 px-5 py-3">{footer}</div>}
        </div>
      )}
    </dialog>
  )
}
