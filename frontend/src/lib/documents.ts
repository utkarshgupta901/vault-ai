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

/** Only PDFs have real pages. DOCX and Markdown "pages" are heading sections and plain text is cut into ~1,500-token
 *  parts (app/ingestion/extract.py), so "p. 3" would point at a page that does not exist in the original file. */
export function hasRealPages(mime: string): boolean {
  return mime === 'application/pdf'
}

/** Chip text for a reference to page `page`: "p. 3" for a PDF; otherwise the stored label ("§ Termination",
 *  "part 2") once it is known, and a neutral "part 3" until then. */
export function pageRefLabel(mime: string, page: number, storedLabel?: string): string {
  if (hasRealPages(mime)) return `p. ${page}`
  return storedLabel ?? `part ${page}`
}
