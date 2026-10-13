import { AlertCircle, CheckCircle2, Copy, FileUp, Loader2, RotateCw, UploadCloud, X } from 'lucide-react'
import { useRef, useState, type DragEvent } from 'react'
import { Link } from 'react-router'
import { ApiError } from '../../api/client'
import { uploadDocument } from '../../api/documents'
import type { DocumentOut } from '../../api/types'
import { useToast } from '../../hooks/useToast'
import { cx } from '../../lib/cx'
import { fmtBytes } from '../../lib/format'
import { Card } from '../ui'

type Outcome = 'queued' | 'duplicate' | 'requeued'

interface UploadItem {
  id: number
  name: string
  size: number
  state: 'uploading' | 'done' | 'error'
  outcome?: Outcome
  documentId?: string
  error?: { code: string; message: string; requestId: string | null }
}

const ACCEPT = '.pdf,.docx,.txt,.md,application/pdf,text/plain,text/markdown,application/vnd.openxmlformats-officedocument.wordprocessingml.document'

const OUTCOME_TEXT: Record<Outcome, string> = {
  queued: 'Queued for processing',
  duplicate: 'Already uploaded - no new cost',
  requeued: 'Previously failed - re-queued',
}

let nextUploadId = 1

/**
 * Drag-and-drop or pick files; each file is its own POST /documents request, so one bad file never blocks the rest.
 * The server (not this component) decides what is acceptable: type sniffing, size and emptiness errors come back as
 * envelopes (415 / 413 / 422) and are shown inline next to the file.
 */
export function UploadPanel({ onUploaded }: { onUploaded: (doc: DocumentOut) => void }) {
  const toast = useToast()
  const [items, setItems] = useState<UploadItem[]>([])
  const [dragging, setDragging] = useState(false)
  const inputRef = useRef<HTMLInputElement>(null)

  const patch = (id: number, change: Partial<UploadItem>) => setItems((list) => list.map((it) => (it.id === id ? { ...it, ...change } : it)))

  async function uploadOne(file: File) {
    const id = nextUploadId++
    setItems((list) => [{ id, name: file.name, size: file.size, state: 'uploading' as const }, ...list].slice(0, 12))
    try {
      const res = await uploadDocument(file)
      const outcome: Outcome = res.requeued ? 'requeued' : res.duplicate ? 'duplicate' : 'queued'
      patch(id, { state: 'done', outcome, documentId: res.document.id })
      onUploaded(res.document)
      if (outcome === 'duplicate') toast.show({ kind: 'info', title: 'Already uploaded - no new cost', message: `${file.name} has the same content (SHA-256) as an existing document.` })
    } catch (err) {
      const e = err instanceof ApiError ? err : null
      patch(id, { state: 'error', error: { code: e?.code ?? 'UPLOAD_FAILED', message: err instanceof Error ? err.message : String(err), requestId: e?.requestId ?? null } })
    }
  }

  function uploadAll(files: FileList | null) {
    for (const file of Array.from(files ?? [])) void uploadOne(file)
  }

  function onDrop(e: DragEvent) {
    e.preventDefault()
    setDragging(false)
    uploadAll(e.dataTransfer.files)
  }

  return (
    <Card className="mb-5">
      <button
        type="button"
        onClick={() => inputRef.current?.click()}
        onDragOver={(e) => {
          e.preventDefault()
          setDragging(true)
        }}
        onDragLeave={(e) => {
          if (!e.currentTarget.contains(e.relatedTarget as Node | null)) setDragging(false)
        }}
        onDrop={onDrop}
        className={cx(
          'm-3 flex w-[calc(100%-1.5rem)] flex-col items-center justify-center rounded-lg border-2 border-dashed px-6 py-7 text-center transition-colors',
          dragging ? 'border-accent-500 bg-accent-50' : 'border-zinc-300 bg-zinc-50/50 hover:border-zinc-400 hover:bg-zinc-50',
        )}
      >
        <UploadCloud className={cx('mb-2 size-7', dragging ? 'text-accent-600' : 'text-zinc-400')} aria-hidden />
        <span className="text-sm font-medium text-zinc-900">Drop files here or click to upload</span>
        <span className="mt-1 text-xs text-zinc-500">PDF, DOCX, TXT or Markdown · up to 25 MB each · processed in the background</span>
      </button>
      <input
        ref={inputRef}
        type="file"
        multiple
        accept={ACCEPT}
        className="hidden"
        onChange={(e) => {
          uploadAll(e.target.files)
          e.target.value = '' // allow picking the same file again (to demo the duplicate path)
        }}
      />
      {items.length > 0 && (
        <div className="border-t border-zinc-100 px-4 py-2">
          <div className="flex items-center justify-between py-1">
            <p className="text-xs font-medium text-zinc-500">Recent uploads</p>
            <button type="button" onClick={() => setItems((list) => list.filter((i) => i.state === 'uploading'))} className="flex items-center gap-1 text-xs text-zinc-500 hover:text-zinc-800">
              <X className="size-3" aria-hidden /> Clear
            </button>
          </div>
          <ul className="divide-y divide-zinc-100">
            {items.map((item) => (
              <UploadRow key={item.id} item={item} />
            ))}
          </ul>
        </div>
      )}
    </Card>
  )
}

function UploadRow({ item }: { item: UploadItem }) {
  const Icon =
    item.state === 'uploading' ? Loader2 : item.state === 'error' ? AlertCircle : item.outcome === 'duplicate' ? Copy : item.outcome === 'requeued' ? RotateCw : CheckCircle2
  const color = item.state === 'error' ? 'text-red-600' : item.state === 'uploading' ? 'text-zinc-400' : item.outcome === 'queued' ? 'text-emerald-600' : 'text-accent-600'
  return (
    <li className="flex items-start gap-3 py-2 text-sm">
      <Icon className={cx('mt-0.5 size-4 shrink-0', color, item.state === 'uploading' && 'animate-spin')} aria-hidden />
      <div className="min-w-0 flex-1">
        <p className="flex items-center gap-2">
          <FileUp className="size-3.5 text-zinc-400" aria-hidden />
          <span className="truncate font-medium text-zinc-800">{item.name}</span>
          <span className="shrink-0 text-xs text-zinc-400">{fmtBytes(item.size)}</span>
        </p>
        {item.state === 'uploading' && <p className="text-xs text-zinc-500">Uploading…</p>}
        {item.state === 'done' && item.outcome && <p className="text-xs text-zinc-600">{OUTCOME_TEXT[item.outcome]}</p>}
        {item.error && (
          <p className="text-xs text-red-700" role="alert">
            <span className="font-mono font-medium">{item.error.code}</span> · {item.error.message}
            {item.error.requestId && <span className="ml-1 font-mono text-zinc-400">({item.error.requestId})</span>}
          </p>
        )}
      </div>
      {item.documentId && (
        <Link to={`/documents/${item.documentId}`} className="shrink-0 text-xs font-medium text-accent-600 hover:text-accent-700">
          Open
        </Link>
      )}
    </li>
  )
}
