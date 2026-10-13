import { ArrowLeft, Download, FileText, MessagesSquare, RotateCw, Sparkles, Trash2 } from 'lucide-react'
import { useCallback, useRef, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router'
import { createSession } from '../api/chat'
import { deleteDocument, downloadOriginal, getDocument, reprocessDocument } from '../api/documents'
import type { DocumentStatusEvent, ReprocessScope } from '../api/types'
import { ConfirmDialog } from '../components/ConfirmDialog'
import { DocumentMeta } from '../components/detail/DocumentMeta'
import { InsightsPanel } from '../components/detail/InsightsPanel'
import { SummaryPanel } from '../components/detail/SummaryPanel'
import { PageViewer } from '../components/PageViewer'
import { StatusChip } from '../components/StatusChip'
import { Button, Card, CardHeader, EmptyState, ErrorState, Skeleton } from '../components/ui'
import { useApi } from '../hooks/useApi'
import { applyStatusEvent, isTerminal, useDocumentEvents } from '../hooks/useDocumentEvents'
import { useToast } from '../hooks/useToast'
import { canReprocess } from '../lib/documents'

export function DocumentDetailPage() {
  const { documentId = '' } = useParams()
  const navigate = useNavigate()
  const toast = useToast()
  const { data: doc, error, reload, setData } = useApi(`doc:${documentId}`, (signal) => getDocument(documentId, signal))
  const [page, setPage] = useState(1)
  const [highlight, setHighlight] = useState<string | undefined>()
  const [busy, setBusy] = useState<null | 'reprocess' | 'ask' | 'download'>(null)
  const [confirmDelete, setConfirmDelete] = useState(false)
  const viewerRef = useRef<HTMLDivElement>(null)

  const onStatus = useCallback(
    (_: string, ev: DocumentStatusEvent) => {
      setData((d) => d && applyStatusEvent(d, ev))
      if (isTerminal(ev)) reload() // pick up page count, category and tags written at the end of processing
    },
    [setData, reload],
  )
  const onDeleted = useCallback(() => navigate('/', { replace: true }), [navigate])
  useDocumentEvents(doc ? [doc] : [], onStatus, onDeleted)

  const openPage = useCallback((n: number, quote?: string) => {
    setPage(n)
    setHighlight(quote)
    viewerRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  }, [])

  if (error && !doc) {
    return (
      <div className="mx-auto max-w-[1400px] px-4 py-6 lg:px-6">
        <BackLink />
        <Card>
          <ErrorState error={error} onRetry={reload} what="this document" />
        </Card>
      </div>
    )
  }
  if (!doc) return <DetailSkeleton />

  async function reprocess(scope: ReprocessScope) {
    setBusy('reprocess')
    try {
      const res = await reprocessDocument(documentId, scope)
      setData(() => res.document)
      toast.show({ kind: 'success', title: scope === 'insights' ? 'Regenerating insights' : 'Reprocessing started' })
    } catch (err) {
      toast.error(err, 'Could not reprocess')
    } finally {
      setBusy(null)
    }
  }

  /** "Ask" on a suggested question: a new chat scoped to this document, which then sends the question. */
  async function ask(question?: string) {
    if (!doc) return
    setBusy('ask')
    try {
      const session = await createSession({ document_ids: [doc.id], all_documents: false, title: doc.title ?? doc.filename })
      navigate(`/chat/${session.id}`, { state: question ? { ask: question } : undefined })
    } catch (err) {
      toast.error(err, 'Could not start a chat')
      setBusy(null)
    }
  }

  async function download() {
    if (!doc) return
    setBusy('download')
    try {
      await downloadOriginal(doc.id, doc.filename)
    } catch (err) {
      toast.error(err, 'Download failed')
    } finally {
      setBusy(null)
    }
  }

  async function remove() {
    try {
      await deleteDocument(documentId)
      toast.show({ kind: 'success', title: 'Document deleted', message: doc?.filename })
      navigate('/', { replace: true })
    } catch (err) {
      toast.error(err, 'Could not delete')
      setConfirmDelete(false)
    }
  }

  const ready = doc.status === 'ready'
  return (
    <div className="mx-auto max-w-[1400px] px-4 py-6 lg:px-6">
      <BackLink />
      <div className="mb-5 flex flex-wrap items-start justify-between gap-4">
        <div className="min-w-0">
          <h1 className="flex items-center gap-2 text-xl font-semibold tracking-tight text-zinc-900">
            <FileText className="size-5 shrink-0 text-zinc-400" aria-hidden />
            <span className="truncate">{doc.filename}</span>
          </h1>
          <div className="mt-2">
            <StatusChip status={doc.status} stage={doc.stage} progress={doc.progress_pct} stale={doc.stale} error={doc.error} />
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Button variant="primary" icon={MessagesSquare} onClick={() => ask()} busy={busy === 'ask'} disabled={!ready}>
            Chat with this document
          </Button>
          <Button icon={Download} onClick={download} busy={busy === 'download'}>
            Original
          </Button>
          <Button icon={RotateCw} onClick={() => reprocess('all')} disabled={!canReprocess(doc) || busy !== null}>
            Reprocess
          </Button>
          <Button icon={Sparkles} onClick={() => reprocess('insights')} disabled={!ready || busy !== null} title="Regenerate AI insights only (no re-embedding)">
            Regenerate insights
          </Button>
          <Button variant="ghost" icon={Trash2} onClick={() => setConfirmDelete(true)} aria-label="Delete document" className="hover:text-red-600" />
        </div>
      </div>

      <div className="grid gap-5 lg:grid-cols-[minmax(0,1fr)_minmax(0,26rem)]">
        <div className="min-w-0 space-y-5">
          {doc.status === 'failed' ? (
            <Card>
              <EmptyState icon={FileText} title="Processing failed" action={doc.error?.retryable || canReprocess(doc) ? <Button onClick={() => reprocess('all')}>Reprocess</Button> : undefined}>
                {doc.error && (
                  <>
                    <span className="font-mono text-xs text-red-600">{doc.error.code}</span>
                    <p>{doc.error.message}</p>
                  </>
                )}
              </EmptyState>
            </Card>
          ) : (
            <InsightsPanel
              documentId={doc.id}
              status={doc.insights_status}
              onOpenPage={openPage}
              onAsk={(q) => ask(q)}
              onRegenerate={() => reprocess('insights')}
              askBusy={busy === 'ask'}
            />
          )}
          <SummaryPanel documentId={doc.id} ready={ready} />
        </div>
        <div className="min-w-0 space-y-5">
          <DocumentMeta doc={doc} />
          <div ref={viewerRef} className="scroll-mt-20">
            <Card>
              <CardHeader title="Page viewer" description="Extracted text, exactly what retrieval and citations see." />
              <div className="p-4">
                {ready ? (
                  <PageViewer
                    documentId={doc.id}
                    page={page}
                    pageCount={doc.page_count}
                    highlight={highlight}
                    onPageChange={(n) => {
                      setPage(n)
                      setHighlight(undefined)
                    }}
                  />
                ) : (
                  <p className="text-sm text-zinc-500">Pages are available once processing finishes.</p>
                )}
              </div>
            </Card>
          </div>
        </div>
      </div>

      <ConfirmDialog open={confirmDelete} title="Delete document?" confirmLabel="Delete" onConfirm={remove} onClose={() => setConfirmDelete(false)}>
        <span className="font-medium text-zinc-900">{doc.filename}</span> and everything derived from it will be permanently deleted.
      </ConfirmDialog>
    </div>
  )
}

function BackLink() {
  return (
    <Link to="/" className="mb-4 inline-flex items-center gap-1 text-sm text-zinc-500 hover:text-zinc-900">
      <ArrowLeft className="size-4" aria-hidden /> Documents
    </Link>
  )
}

function DetailSkeleton() {
  return (
    <div className="mx-auto max-w-[1400px] px-4 py-6 lg:px-6" role="status" aria-label="Loading document">
      <Skeleton className="mb-4 h-4 w-24" />
      <Skeleton className="mb-6 h-7 w-80" />
      <div className="grid gap-5 lg:grid-cols-[minmax(0,1fr)_minmax(0,26rem)]">
        <Skeleton className="h-96" />
        <Skeleton className="h-96" />
      </div>
    </div>
  )
}
