import { FileText, Library } from 'lucide-react'
import { useState } from 'react'
import { createSession } from '../../api/chat'
import { ApiError } from '../../api/client'
import type { DocumentOut, SessionOut } from '../../api/types'
import { cx } from '../../lib/cx'
import { Dialog } from '../Dialog'
import { Button, Spinner } from '../ui'

const MAX_DOCS = 20 // SessionCreate.document_ids max_length

interface NewSessionDialogProps {
  open: boolean
  onClose: () => void
  onCreated: (session: SessionOut) => void
  readyDocs: DocumentOut[] | undefined
}

/** Scope picker: a chat searches either all of the owner's ready documents or a chosen subset. */
export function NewSessionDialog({ open, onClose, onCreated, readyDocs }: NewSessionDialogProps) {
  const [scope, setScope] = useState<'all' | 'selected'>('all')
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [title, setTitle] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<ApiError | Error | null>(null)

  function toggle(id: string) {
    setSelected((s) => {
      const next = new Set(s)
      if (next.has(id)) next.delete(id)
      else if (next.size < MAX_DOCS) next.add(id)
      return next
    })
  }

  async function create() {
    setBusy(true)
    setError(null)
    try {
      const session = await createSession({
        all_documents: scope === 'all',
        document_ids: scope === 'all' ? [] : [...selected],
        title: title.trim() || null,
      })
      onCreated(session)
    } catch (err) {
      setError(err instanceof Error ? err : new Error(String(err)))
    } finally {
      setBusy(false)
    }
  }

  const canCreate = scope === 'all' || selected.size > 0
  return (
    <Dialog
      open={open}
      onClose={onClose}
      title="New chat"
      description="Answers cite the pages they come from."
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button variant="primary" onClick={create} busy={busy} disabled={!canCreate}>
            Start chat
          </Button>
        </>
      }
    >
      <div className="space-y-4">
        <fieldset className="grid grid-cols-2 gap-2">
          <legend className="sr-only">Scope</legend>
          <ScopeOption checked={scope === 'all'} onSelect={() => setScope('all')} icon={Library} title="All documents" text="Search every ready document" />
          <ScopeOption checked={scope === 'selected'} onSelect={() => setScope('selected')} icon={FileText} title="Selected documents" text={`Pick up to ${MAX_DOCS}`} />
        </fieldset>

        {scope === 'selected' && (
          <div className="max-h-64 overflow-y-auto rounded-md border border-zinc-200">
            {!readyDocs ? (
              <div className="flex justify-center p-4">
                <Spinner />
              </div>
            ) : readyDocs.length === 0 ? (
              <p className="p-4 text-center text-sm text-zinc-500">No ready documents yet. Upload one and wait for it to finish processing.</p>
            ) : (
              <ul className="divide-y divide-zinc-100">
                {readyDocs.map((d) => (
                  <li key={d.id}>
                    <label className="flex cursor-pointer items-center gap-3 px-3 py-2 text-sm hover:bg-zinc-50">
                      <input type="checkbox" checked={selected.has(d.id)} onChange={() => toggle(d.id)} className="size-4 accent-accent-600" />
                      <span className="min-w-0 flex-1 truncate text-zinc-800">{d.filename}</span>
                      {d.category && <span className="text-xs text-zinc-400">{d.category}</span>}
                    </label>
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}

        <label className="block text-sm">
          <span className="text-xs text-zinc-500">Title (optional)</span>
          <input
            value={title}
            onChange={(e) => setTitle(e.target.value.slice(0, 200))}
            placeholder="e.g. Vendor comparison"
            className="mt-1 h-9 w-full rounded-md border border-zinc-300 px-2.5 text-sm focus:border-accent-500 focus:outline-none"
          />
        </label>

        {error && (
          <p className="rounded-md bg-red-50 px-3 py-2 text-sm text-red-700" role="alert">
            {error instanceof ApiError && <span className="mr-1 font-mono text-xs font-semibold">{error.code}</span>}
            {error.message}
          </p>
        )}
      </div>
    </Dialog>
  )
}

function ScopeOption({ checked, onSelect, icon: Icon, title, text }: { checked: boolean; onSelect: () => void; icon: typeof Library; title: string; text: string }) {
  return (
    <label className={cx('flex cursor-pointer gap-2.5 rounded-lg border p-3 has-focus-visible:ring-2 has-focus-visible:ring-accent-500', checked ? 'border-accent-500 bg-accent-50' : 'border-zinc-200 hover:bg-zinc-50')}>
      <input type="radio" name="scope" checked={checked} onChange={onSelect} className="sr-only" />
      <Icon className={cx('mt-0.5 size-4 shrink-0', checked ? 'text-accent-600' : 'text-zinc-400')} aria-hidden />
      <span>
        <span className="block text-sm font-medium text-zinc-900">{title}</span>
        <span className="block text-xs text-zinc-500">{text}</span>
      </span>
    </label>
  )
}
