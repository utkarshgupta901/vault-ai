import { ExternalLink, FileText, RotateCw, Trash2 } from 'lucide-react'
import { Link } from 'react-router'
import type { DocumentOut } from '../../api/types'
import { canReprocess, mimeLabel } from '../../lib/documents'
import { fmtBytes, fmtDateTime, fmtNumber, fmtRelative, humanize } from '../../lib/format'
import { InsightsChip, StatusChip } from '../StatusChip'
import { Badge } from '../ui'

interface DocumentTableProps {
  docs: DocumentOut[]
  busyIds: Set<string>
  onDelete: (doc: DocumentOut) => void
  onReprocess: (doc: DocumentOut) => void
}

export function DocumentTable({ docs, busyIds, onDelete, onReprocess }: DocumentTableProps) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[960px] text-sm">
        <thead>
          <tr className="border-b border-zinc-100 text-left text-xs font-medium text-zinc-500">
            <th className="px-4 py-2.5 font-medium">Document</th>
            <th className="px-3 py-2.5 font-medium">Status</th>
            <th className="px-3 py-2.5 font-medium">Insights</th>
            <th className="px-3 py-2.5 font-medium">Category</th>
            <th className="px-3 py-2.5 font-medium">Tags</th>
            <th className="px-3 py-2.5 text-right font-medium">Pages</th>
            <th className="px-3 py-2.5 font-medium">Created</th>
            <th className="px-4 py-2.5 text-right font-medium">
              <span className="sr-only">Actions</span>
            </th>
          </tr>
        </thead>
        <tbody className="divide-y divide-zinc-100">
          {docs.map((doc) => (
            <DocumentRow key={doc.id} doc={doc} busy={busyIds.has(doc.id)} onDelete={onDelete} onReprocess={onReprocess} />
          ))}
        </tbody>
      </table>
    </div>
  )
}

function DocumentRow({ doc, busy, onDelete, onReprocess }: { doc: DocumentOut; busy: boolean; onDelete: (d: DocumentOut) => void; onReprocess: (d: DocumentOut) => void }) {
  const reprocessable = canReprocess(doc)
  return (
    <tr className="align-top hover:bg-zinc-50/60">
      <td className="max-w-80 px-4 py-3">
        <Link to={`/documents/${doc.id}`} className="group flex items-start gap-2">
          <FileText className="mt-0.5 size-4 shrink-0 text-zinc-400" aria-hidden />
          <span className="min-w-0">
            <span className="block truncate font-medium text-zinc-900 group-hover:text-accent-700" title={doc.filename}>
              {doc.filename}
            </span>
            <span className="block text-xs text-zinc-500">
              {mimeLabel(doc.mime_type)} · {fmtBytes(doc.size_bytes)}
              {doc.upload_count > 1 && <span title="Re-uploads of identical bytes are deduplicated (no new cost)"> · uploaded {doc.upload_count}×</span>}
            </span>
          </span>
        </Link>
      </td>
      <td className="px-3 py-3">
        <StatusChip status={doc.status} stage={doc.stage} progress={doc.progress_pct} stale={doc.stale} error={doc.error} />
      </td>
      <td className="px-3 py-3">{doc.status === 'failed' ? <span className="text-xs text-zinc-400">—</span> : <InsightsChip status={doc.insights_status} errorCode={doc.insights_error_code} />}</td>
      <td className="px-3 py-3 text-zinc-700">{doc.category ? humanize(doc.category) : <span className="text-zinc-400">—</span>}</td>
      <td className="max-w-56 px-3 py-3">
        <div className="flex flex-wrap gap-1">
          {doc.tags.slice(0, 4).map((t) => (
            <Badge key={t}>{t}</Badge>
          ))}
          {doc.tags.length > 4 && <span className="text-xs text-zinc-400">+{doc.tags.length - 4}</span>}
          {doc.tags.length === 0 && <span className="text-zinc-400">—</span>}
        </div>
      </td>
      <td className="px-3 py-3 text-right text-zinc-700 tabular-nums">{fmtNumber(doc.page_count)}</td>
      <td className="px-3 py-3 whitespace-nowrap text-zinc-500" title={fmtDateTime(doc.created_at)}>
        {fmtRelative(doc.created_at)}
      </td>
      <td className="px-4 py-3">
        <div className="flex justify-end gap-0.5">
          <Link to={`/documents/${doc.id}`} className="rounded p-1.5 text-zinc-500 hover:bg-zinc-100 hover:text-zinc-900" title="Open" aria-label={`Open ${doc.filename}`}>
            <ExternalLink className="size-4" />
          </Link>
          <button
            type="button"
            disabled={!reprocessable || busy}
            onClick={() => onReprocess(doc)}
            className="rounded p-1.5 text-zinc-500 hover:bg-zinc-100 hover:text-zinc-900 disabled:cursor-not-allowed disabled:opacity-40"
            title={reprocessable ? 'Reprocess' : 'Already processing'}
            aria-label={`Reprocess ${doc.filename}`}
          >
            <RotateCw className={busy ? 'size-4 animate-spin' : 'size-4'} />
          </button>
          <button
            type="button"
            disabled={busy}
            onClick={() => onDelete(doc)}
            className="rounded p-1.5 text-zinc-500 hover:bg-red-50 hover:text-red-600 disabled:opacity-40"
            title="Delete"
            aria-label={`Delete ${doc.filename}`}
          >
            <Trash2 className="size-4" />
          </button>
        </div>
      </td>
    </tr>
  )
}
