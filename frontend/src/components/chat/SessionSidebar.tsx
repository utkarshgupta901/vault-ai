import { Library, MessageSquare, Plus, Trash2 } from 'lucide-react'
import { NavLink } from 'react-router'
import type { SessionOut } from '../../api/types'
import { cx } from '../../lib/cx'
import { fmtRelative } from '../../lib/format'
import { sessionLabel } from '../../lib/sessions'
import { Button, ErrorState, Skeleton } from '../ui'

interface SessionSidebarProps {
  sessions: SessionOut[] | undefined
  error: unknown
  onRetry: () => void
  onNew: () => void
  onDelete: (session: SessionOut) => void
  docNames: Map<string, string>
}

export function SessionSidebar({ sessions, error, onRetry, onNew, onDelete, docNames }: SessionSidebarProps) {
  return (
    <aside className="flex w-72 shrink-0 flex-col border-r border-zinc-200 bg-white">
      <div className="p-3">
        <Button variant="primary" icon={Plus} onClick={onNew} className="w-full">
          New chat
        </Button>
      </div>
      <nav className="min-h-0 flex-1 overflow-y-auto px-2 pb-3" aria-label="Chat sessions">
        {error && !sessions ? (
          <ErrorState error={error} onRetry={onRetry} what="chats" />
        ) : !sessions ? (
          <div className="space-y-2 p-2">
            {Array.from({ length: 4 }, (_, i) => (
              <Skeleton key={i} className="h-10" />
            ))}
          </div>
        ) : sessions.length === 0 ? (
          <p className="px-3 py-6 text-center text-xs text-zinc-500">No chats yet. Start one to ask questions across your documents.</p>
        ) : (
          <ul className="space-y-0.5">
            {sessions.map((s) => (
              <li key={s.id} className="group relative">
                <NavLink
                  to={`/chat/${s.id}`}
                  className={({ isActive }) =>
                    cx('flex items-start gap-2 rounded-md py-2 pr-8 pl-2.5 text-sm', isActive ? 'bg-zinc-100 text-zinc-900' : 'text-zinc-600 hover:bg-zinc-50 hover:text-zinc-900')
                  }
                >
                  {s.all_documents ? <Library className="mt-0.5 size-4 shrink-0 text-zinc-400" aria-hidden /> : <MessageSquare className="mt-0.5 size-4 shrink-0 text-zinc-400" aria-hidden />}
                  <span className="min-w-0">
                    <span className="block truncate font-medium">{sessionLabel(s, docNames)}</span>
                    <span className="block text-xs text-zinc-400">
                      {s.all_documents ? 'All documents' : `${s.document_ids.length} doc${s.document_ids.length === 1 ? '' : 's'}`} · {fmtRelative(s.last_message_at ?? s.created_at)}
                    </span>
                  </span>
                </NavLink>
                <button
                  type="button"
                  onClick={() => onDelete(s)}
                  className="absolute top-2 right-1.5 rounded p-1 text-zinc-400 opacity-0 group-focus-within:opacity-100 group-hover:opacity-100 hover:bg-red-50 hover:text-red-600 focus-visible:opacity-100"
                  aria-label={`Delete chat ${sessionLabel(s, docNames)}`}
                >
                  <Trash2 className="size-3.5" />
                </button>
              </li>
            ))}
          </ul>
        )}
      </nav>
    </aside>
  )
}
