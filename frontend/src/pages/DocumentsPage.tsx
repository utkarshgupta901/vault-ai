import { FileSearch, FolderOpen, RefreshCw } from 'lucide-react'
import { useCallback, useState } from 'react'
import { deleteDocument, getDocument, listDocuments, reprocessDocument, type DocumentFilters as Filters } from '../api/documents'
import type { DocumentOut, DocumentStatusEvent, Page } from '../api/types'
import { ConfirmDialog } from '../components/ConfirmDialog'
import { DocumentFilters } from '../components/documents/DocumentFilters'
import { DocumentTable } from '../components/documents/DocumentTable'
import { UploadPanel } from '../components/documents/UploadPanel'
import { Button, Card, EmptyState, ErrorState, PageHeader, SkeletonRows } from '../components/ui'
import { useApi, useDebounced } from '../hooks/useApi'
import { applyStatusEvent, isTerminal, useDocumentEvents } from '../hooks/useDocumentEvents'
import { useToast } from '../hooks/useToast'
import { pluralize } from '../lib/format'

type PageUpdate = (prev: Page<DocumentOut> | undefined) => Page<DocumentOut> | undefined

/** Pure list updates, kept outside the component so each one is easy to read and test. */
const replaceDoc = (doc: DocumentOut): PageUpdate => (p) => p && { ...p, items: p.items.map((d) => (d.id === doc.id ? doc : d)) }
const upsertDoc = (doc: DocumentOut): PageUpdate => (p) =>
  p && (p.items.some((d) => d.id === doc.id) ? replaceDoc(doc)(p) : { ...p, items: [doc, ...p.items] })
const removeDoc = (id: string): PageUpdate => (p) => p && { ...p, items: p.items.filter((d) => d.id !== id) }

export function DocumentsPage() {
  const toast = useToast()
  const [filters, setFilters] = useState<Filters>({})
  const debounced = useDebounced(filters, 300)
  const list = useApi(`documents:${JSON.stringify(debounced)}`, (signal) => listDocuments(debounced, null, signal))
  const { setData } = list
  const docs = list.data?.items ?? []
  const [loadingMore, setLoadingMore] = useState(false)
  const [busyIds, setBusyIds] = useState<Set<string>>(new Set())
  const [toDelete, setToDelete] = useState<DocumentOut | null>(null)

  const onStatus = useCallback(
    (id: string, ev: DocumentStatusEvent) => {
      setData((p) => p && { ...p, items: p.items.map((d) => (d.id === id ? applyStatusEvent(d, ev) : d)) })
      // Status events carry only progress fields; on completion re-read the row for pages, category and tags.
      if (isTerminal(ev)) getDocument(id).then((doc) => setData(replaceDoc(doc)), () => undefined)
    },
    [setData],
  )
  const onDeleted = useCallback((id: string) => setData(removeDoc(id)), [setData])
  useDocumentEvents(docs, onStatus, onDeleted)

  const setBusy = (id: string, busy: boolean) =>
    setBusyIds((s) => {
      const next = new Set(s)
      if (busy) next.add(id)
      else next.delete(id)
      return next
    })

  async function loadMore() {
    const cursor = list.data?.next_cursor
    if (!cursor) return
    setLoadingMore(true)
    try {
      const page = await listDocuments(debounced, cursor)
      setData((p) => {
        if (!p) return page
        const seen = new Set(p.items.map((d) => d.id))
        return { items: [...p.items, ...page.items.filter((d) => !seen.has(d.id))], next_cursor: page.next_cursor }
      })
    } catch (err) {
      toast.error(err)
    } finally {
      setLoadingMore(false)
    }
  }

  async function reprocess(doc: DocumentOut) {
    setBusy(doc.id, true)
    try {
      const res = await reprocessDocument(doc.id, 'all')
      setData(replaceDoc(res.document))
      toast.show({ kind: 'success', title: 'Reprocessing started', message: doc.filename })
    } catch (err) {
      toast.error(err, 'Could not reprocess')
    } finally {
      setBusy(doc.id, false)
    }
  }

  async function confirmDelete() {
    if (!toDelete) return
    const doc = toDelete
    try {
      await deleteDocument(doc.id)
      setData(removeDoc(doc.id))
      toast.show({ kind: 'success', title: 'Document deleted', message: doc.filename })
    } catch (err) {
      toast.error(err, 'Could not delete')
    } finally {
      setToDelete(null)
    }
  }

  const filtered = Boolean(debounced.status || debounced.category || debounced.tag || debounced.q)
  return (
    <div className="mx-auto max-w-[1400px] px-4 py-6 lg:px-6">
      <PageHeader
        title="Documents"
        description="Uploads return immediately; extraction, chunking, embeddings and AI insights run in the background."
        actions={
          <Button icon={RefreshCw} onClick={list.reload} busy={list.loading && !!list.data}>
            Refresh
          </Button>
        }
      />
      <UploadPanel onUploaded={(doc) => setData(upsertDoc(doc))} />
      <Card>
        <DocumentFilters value={filters} onChange={setFilters} />
        {list.error && !list.data ? (
          <ErrorState error={list.error} onRetry={list.reload} what="documents" />
        ) : !list.data ? (
          <SkeletonRows rows={5} />
        ) : docs.length === 0 ? (
          filtered ? (
            <EmptyState icon={FileSearch} title="No documents match these filters">
              Try clearing a filter or searching for part of the filename.
            </EmptyState>
          ) : (
            <EmptyState icon={FolderOpen} title="No documents yet">
              Drop a PDF, DOCX, TXT or Markdown file above. You can chat with it as soon as it is ready.
            </EmptyState>
          )
        ) : (
          <DocumentTable docs={docs} busyIds={busyIds} onDelete={setToDelete} onReprocess={reprocess} />
        )}
        {list.data && docs.length > 0 && (
          <div className="flex items-center justify-between border-t border-zinc-100 px-4 py-2.5 text-xs text-zinc-500">
            <span>
              Showing {pluralize(docs.length, 'document')}
              {list.data.next_cursor ? '' : ' · end of list'}
            </span>
            {list.data.next_cursor && (
              <Button size="sm" onClick={loadMore} busy={loadingMore}>
                Load more
              </Button>
            )}
          </div>
        )}
      </Card>
      <ConfirmDialog open={toDelete !== null} title="Delete document?" confirmLabel="Delete" onConfirm={confirmDelete} onClose={() => setToDelete(null)}>
        <p>
          <span className="font-medium text-zinc-900">{toDelete?.filename}</span> and its pages, chunks, embeddings and insights will be permanently deleted.
          Chat answers that cited it keep their text but lose the quoted snippet.
        </p>
      </ConfirmDialog>
    </div>
  )
}
