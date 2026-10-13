import type { DocumentOut } from '../api/types'

/** The server allows reprocessing from ready/failed, or from queued/processing once the document is stale. */
export function canReprocess(doc: Pick<DocumentOut, 'status' | 'stale'>): boolean {
  return doc.status === 'ready' || doc.status === 'failed' || doc.stale
}

const MIME_LABELS: Record<string, string> = {
  'application/pdf': 'PDF',
  'application/vnd.openxmlformats-officedocument.wordprocessingml.document': 'DOCX',
  'text/plain': 'TXT',
  'text/markdown': 'Markdown',
}

export function mimeLabel(mime: string): string {
  return MIME_LABELS[mime] ?? mime
}
