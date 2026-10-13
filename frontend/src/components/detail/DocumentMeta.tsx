import type { DocumentOut } from '../../api/types'
import { mimeLabel } from '../../lib/documents'
import { fmtBytes, fmtDate, fmtDateTime, fmtNumber, humanize } from '../../lib/format'
import { Badge, Card, CardHeader, Field } from '../ui'

/** Metadata from GET /documents/{id}. */
export function DocumentMeta({ doc }: { doc: DocumentOut }) {
  return (
    <Card>
      <CardHeader title="Details" />
      <dl className="grid grid-cols-2 gap-x-4 gap-y-3 p-4">
        <Field label="Type">{mimeLabel(doc.mime_type)}</Field>
        <Field label="Size">{fmtBytes(doc.size_bytes)}</Field>
        <Field label="Pages">{fmtNumber(doc.page_count)}</Field>
        <Field label="Chunks">{fmtNumber(doc.chunk_count)}</Field>
        <Field label="Tokens">{fmtNumber(doc.token_count)}</Field>
        <Field label="Category">{humanize(doc.category)}</Field>
        <Field label="Document date">{fmtDate(doc.document_date)}</Field>
        <Field label="Uploads">{doc.upload_count > 1 ? `${doc.upload_count}× (deduplicated)` : '1'}</Field>
        <Field label="Created">{fmtDateTime(doc.created_at)}</Field>
        <Field label="Ready">{fmtDateTime(doc.ready_at)}</Field>
        <div className="col-span-2 min-w-0">
          <dt className="text-xs text-zinc-500">SHA-256</dt>
          <dd className="mt-0.5 truncate font-mono text-xs text-zinc-600" title={doc.sha256}>
            {doc.sha256}
          </dd>
        </div>
        {doc.tags.length > 0 && (
          <div className="col-span-2">
            <dt className="text-xs text-zinc-500">Tags</dt>
            <dd className="mt-1 flex flex-wrap gap-1">
              {doc.tags.map((t) => (
                <Badge key={t}>{t}</Badge>
              ))}
            </dd>
          </div>
        )}
        {doc.warnings.length > 0 && (
          <div className="col-span-2">
            <dt className="text-xs text-zinc-500">Processing warnings</dt>
            <dd className="mt-1 space-y-1">
              {doc.warnings.map((w, i) => (
                <p key={i} className="rounded bg-amber-50 px-2 py-1 font-mono text-xs text-amber-800">
                  {typeof w === 'string' ? w : JSON.stringify(w)}
                </p>
              ))}
            </dd>
          </div>
        )}
      </dl>
    </Card>
  )
}
