import { Library, MessagesSquare, Plus, TriangleAlert } from 'lucide-react'
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { useLocation, useNavigate, useParams } from 'react-router'
import { deleteSession, getSession, listSessions } from '../api/chat'
import { listDocuments } from '../api/documents'
import type { MessageOut, SessionOut } from '../api/types'
import { AssistantMessage, UserMessage } from '../components/chat/Messages'
import { Composer } from '../components/chat/Composer'
import { NewSessionDialog } from '../components/chat/NewSessionDialog'
import { SessionSidebar } from '../components/chat/SessionSidebar'
import { ConfirmDialog } from '../components/ConfirmDialog'
import { PageViewerModal, type PageTarget } from '../components/PageViewer'
import { Badge, Button, EmptyState, ErrorState, Skeleton } from '../components/ui'
import { useApi } from '../hooks/useApi'
import { useChat } from '../hooks/useChat'
import { useToast } from '../hooks/useToast'
import { fromLive, fromMessage, questionFor, warningText } from '../lib/answers'
import type { CitationInfo } from '../lib/citations'
import { sessionLabel } from '../lib/sessions'

/** Navigation state set by the document page's "Ask" button: the question to send once the chat has loaded. */
interface AskState {
  ask?: string
}

export function ChatPage() {
  const { sessionId } = useParams()
  const navigate = useNavigate()
  const location = useLocation()
  const toast = useToast()
  const sessions = useApi('sessions', (signal) => listSessions(signal))
  const readyDocs = useApi('ready-docs', (signal) => listDocuments({ status: 'ready' }, null, signal, 100))
  const session = useApi(sessionId ? `session:${sessionId}` : null, (signal) => getSession(sessionId!, signal))
  const chat = useChat(sessionId)
  const [stream, setStream] = useState(true)
  const [newOpen, setNewOpen] = useState(false)
  const [toDelete, setToDelete] = useState<SessionOut | null>(null)
  const [viewer, setViewer] = useState<PageTarget | null>(null)
  const docNames = useMemo(() => new Map((readyDocs.data?.items ?? []).map((d) => [d.id, d.filename])), [readyDocs.data])

  const { ask: askChat } = chat
  const { reload: reloadSessions } = sessions
  const ask = useCallback(
    async (question: string) => {
      await askChat(question, stream)
      reloadSessions() // last_message_at changed
    },
    [askChat, stream, reloadSessions],
  )

  // Send the question handed over by the document page exactly once, after the session and its history have loaded
  // (so it runs on an update, never during mount), then clear it so a page refresh does not ask again.
  const pendingAsk = (location.state as AskState | null)?.ask
  const askedFor = useRef<string | null>(null)
  const sessionReady = session.data?.id === sessionId && !chat.loading
  useEffect(() => {
    if (!pendingAsk || !sessionReady || askedFor.current === location.key) return
    askedFor.current = location.key
    navigate(location.pathname, { replace: true, state: null })
    void ask(pendingAsk)
  }, [pendingAsk, sessionReady, location.key, location.pathname, navigate, ask])

  const openPage = useCallback((info: CitationInfo) => {
    if (info.documentId && info.pageNo != null) {
      setViewer({ documentId: info.documentId, page: info.pageNo, highlight: info.snippet, filename: info.filename })
    }
  }, [])

  async function confirmDelete() {
    if (!toDelete) return
    const target = toDelete
    try {
      await deleteSession(target.id)
      sessions.setData((p) => p && { ...p, items: p.items.filter((s) => s.id !== target.id) })
      if (target.id === sessionId) navigate('/chat')
    } catch (err) {
      toast.error(err, 'Could not delete chat')
    } finally {
      setToDelete(null)
    }
  }

  function onCreated(created: SessionOut) {
    setNewOpen(false)
    sessions.setData((p) => p && { ...p, items: [created, ...p.items.filter((s) => s.id !== created.id)] })
    navigate(`/chat/${created.id}`)
  }

  // Fills the viewport below the sticky header (h-14 + 1px border) so only the message list scrolls, not the page.
  return (
    <div className="flex h-[calc(100dvh-3.5rem-1px)]">
      <SessionSidebar
        sessions={sessions.data?.items}
        error={sessions.error}
        onRetry={sessions.reload}
        onNew={() => setNewOpen(true)}
        onDelete={setToDelete}
        docNames={docNames}
      />
      <main className="flex min-w-0 flex-1 flex-col bg-zinc-50">
        {!sessionId ? (
          <div className="flex flex-1 items-center justify-center">
            <EmptyState
              icon={MessagesSquare}
              title="Chat with your documents"
              action={
                <Button variant="primary" icon={Plus} onClick={() => setNewOpen(true)}>
                  New chat
                </Button>
              }
            >
              Ask across all documents or a selection. Every answer cites the page it came from; follow-up questions keep the context.
            </EmptyState>
          </div>
        ) : session.error && !session.data ? (
          <div className="flex flex-1 items-center justify-center">
            <ErrorState error={session.error} onRetry={session.reload} what="this chat" />
          </div>
        ) : (
          <>
            <SessionHeader session={session.data} docNames={docNames} />
            <Conversation
              messages={chat.messages}
              loading={chat.loading && chat.messages.length === 0}
              loadError={chat.loadError}
              live={chat.live}
              doneById={chat.doneById}
              olderCursor={chat.olderCursor}
              starters={session.data?.starter_questions ?? []}
              onLoadOlder={chat.loadOlder}
              onAsk={ask}
              onRetry={chat.retry}
              onOpenPage={openPage}
              onReload={() => void chat.reload()}
            />
            <Composer busy={chat.live?.status === 'streaming'} stream={stream} onStreamChange={setStream} onSend={(q) => void ask(q)} onStop={chat.stop} />
          </>
        )}
      </main>

      {newOpen && <NewSessionDialog open onClose={() => setNewOpen(false)} onCreated={onCreated} readyDocs={readyDocs.data?.items} />}
      <ConfirmDialog open={toDelete !== null} title="Delete chat?" confirmLabel="Delete" onConfirm={confirmDelete} onClose={() => setToDelete(null)}>
        The conversation and its answers will be permanently deleted. Your documents are not affected.
      </ConfirmDialog>
      <PageViewerModal target={viewer} onClose={() => setViewer(null)} onPageChange={(page) => setViewer((v) => v && { ...v, page, highlight: null })} />
    </div>
  )
}

function SessionHeader({ session, docNames }: { session: SessionOut | undefined; docNames: Map<string, string> }) {
  if (!session) {
    return (
      <div className="border-b border-zinc-200 bg-white px-5 py-3">
        <Skeleton className="h-5 w-56" />
      </div>
    )
  }
  return (
    <div className="border-b border-zinc-200 bg-white px-5 py-3">
      <h1 className="truncate text-sm font-semibold text-zinc-900">{sessionLabel(session, docNames)}</h1>
      <div className="mt-1 flex flex-wrap items-center gap-1.5">
        {session.all_documents ? (
          <Badge icon={Library}>All ready documents</Badge>
        ) : (
          session.document_ids.map((id) => <Badge key={id}>{docNames.get(id) ?? 'Document (processing or removed)'}</Badge>)
        )}
      </div>
      {session.warnings.length > 0 && (
        <ul className="mt-1.5 space-y-0.5">
          {session.warnings.map((w) => (
            <li key={w} className="flex items-start gap-1.5 text-xs text-amber-800">
              <TriangleAlert className="mt-0.5 size-3.5 shrink-0" aria-hidden />
              <span>
                {warningText(w)} <span className="font-mono text-amber-600/70">({w})</span>
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

interface ConversationProps {
  messages: MessageOut[]
  loading: boolean
  loadError: unknown
  live: ReturnType<typeof useChat>['live']
  doneById: ReturnType<typeof useChat>['doneById']
  olderCursor: string | null
  starters: string[]
  onLoadOlder: () => void
  onAsk: (q: string) => void
  onRetry: () => void
  onOpenPage: (info: CitationInfo) => void
  onReload: () => void
}

/** The scrolling message list. Sticks to the bottom while an answer streams, unless the user scrolled up to read. */
function Conversation({ messages, loading, loadError, live, doneById, olderCursor, starters, onLoadOlder, onAsk, onRetry, onOpenPage, onReload }: ConversationProps) {
  const scroller = useRef<HTMLDivElement>(null)
  const pinned = useRef(true)

  // While the live turn exists the server may already have stored its rows; never show them twice.
  const liveIds = new Set([live?.meta?.user_message_id, live?.meta?.message_id].filter(Boolean))
  const visible = messages.filter((m) => !liveIds.has(m.id))
  const lastAssistantIndex = visible.findLastIndex((m) => m.role === 'assistant')

  useLayoutEffect(() => {
    const el = scroller.current
    if (el && pinned.current) el.scrollTop = el.scrollHeight
  }, [messages, live])

  useEffect(() => {
    if (live?.status === 'streaming') pinned.current = true // a new question always scrolls into view
  }, [live?.clientMessageId, live?.status])

  return (
    <div
      ref={scroller}
      onScroll={(e) => {
        const el = e.currentTarget
        pinned.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80
      }}
      className="min-h-0 flex-1 overflow-y-auto"
    >
      <div className="mx-auto max-w-3xl space-y-6 px-4 py-6" aria-live="polite">
        {olderCursor && (
          <div className="text-center">
            <Button size="sm" variant="ghost" onClick={onLoadOlder}>
              Load earlier messages
            </Button>
          </div>
        )}
        {loading ? (
          <div className="space-y-4">
            <Skeleton className="ml-auto h-10 w-1/2" />
            <Skeleton className="h-24 w-4/5" />
          </div>
        ) : loadError ? (
          <ErrorState error={loadError} onRetry={onReload} what="messages" />
        ) : visible.length === 0 && !live ? (
          <Starters starters={starters} onAsk={onAsk} />
        ) : null}

        {visible.map((m, i) =>
          m.role === 'user' ? (
            <UserMessage key={m.id} content={m.content} />
          ) : (
            <AssistantMessage
              key={m.id}
              view={fromMessage(m, questionFor(visible, i), doneById[m.id])}
              isLatest={!live && i === lastAssistantIndex}
              onAsk={onAsk}
              onOpenPage={onOpenPage}
              onRetry={m.status === 'failed' || m.status === 'interrupted' ? () => onAsk(questionFor(visible, i) ?? '') : undefined}
            />
          ),
        )}

        {live && (
          <>
            <UserMessage content={live.question} />
            <AssistantMessage
              view={fromLive(live, live.meta ? doneById[live.meta.message_id] : undefined)}
              isLatest
              onAsk={onAsk}
              onOpenPage={onOpenPage}
              onRetry={live.status === 'error' || live.status === 'stopped' ? onRetry : undefined}
            />
          </>
        )}
      </div>
    </div>
  )
}

function Starters({ starters, onAsk }: { starters: string[]; onAsk: (q: string) => void }) {
  return (
    <div className="py-10 text-center">
      <MessagesSquare className="mx-auto mb-3 size-6 text-zinc-400" aria-hidden />
      <p className="text-sm font-medium text-zinc-900">Ask anything about the documents in this chat</p>
      <p className="mt-1 text-sm text-zinc-500">Answers cite their sources; click a citation to see the page.</p>
      {starters.length > 0 && (
        <div className="mt-5 flex flex-wrap justify-center gap-2">
          {starters.map((q) => (
            <button key={q} type="button" onClick={() => onAsk(q)} className="rounded-full border border-zinc-200 bg-white px-3.5 py-1.5 text-sm text-zinc-700 shadow-xs hover:border-accent-500 hover:text-accent-700">
              {q}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}
