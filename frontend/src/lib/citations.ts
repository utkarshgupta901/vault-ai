/**
 * Citation markers in answers look like [S1] or [S1, S3]; each Sn points at one source chunk sent to the model.
 *
 * Two problems are solved here:
 * 1. Rendering: markers are rewritten into markdown links with a fragment URL (#cite-S1). react-markdown passes links
 *    to our own <a> renderer, which draws a chip instead. A fragment URL survives react-markdown's URL sanitiser, and
 *    the answer text never needs raw HTML.
 * 2. Streaming: tokens arrive in arbitrary pieces, so a delta can end in the middle of a marker ("... 18 days [S").
 *    Rendering that tail would flash "[S" as text and then turn it into a chip a moment later. While streaming we
 *    therefore hold back an incomplete marker at the end of the text until the next delta completes (or rejects) it.
 */
import type { Citation, SourceRef } from '../api/types'

const MARKER = /\[(S\d+(?:\s*,\s*S\d+)*)\]/g
const INCOMPLETE_TAIL = /\[(?:S(?:\d+(?:\s*,\s*(?:S\d*)?)*)?)?$/

/** Splits streamed text into the part that is safe to render now and a pending incomplete marker. */
export function splitIncompleteMarker(text: string): { stable: string; pending: string } {
  const m = INCOMPLETE_TAIL.exec(text)
  if (!m) return { stable: text, pending: '' }
  return { stable: text.slice(0, m.index), pending: m[0] }
}

export function linkifyCitations(text: string): string {
  return text.replace(MARKER, (_, refs: string) =>
    refs
      .split(/\s*,\s*/)
      .map((ref) => `[${ref}](#cite-${ref})`)
      .join(''),
  )
}

export const CITE_HREF_PREFIX = '#cite-'

/** Everything a chip needs. `pageNo` is known only once the citations event (or a stored message) provides it. */
export interface CitationInfo {
  ref: string
  documentId: string | null
  filename: string
  label: string
  pageNo: number | null
  snippet: string | null
  weakSupport: boolean
  sourceDeleted: boolean
  /** False when the model cited an id that was not among the sources it was given. */
  valid: boolean
}

/** Resolves a ref against the checked citations first, then the raw sources from the meta event. */
export function buildCitationLookup(citations: Citation[], sources: SourceRef[] = []) {
  const map = new Map<string, CitationInfo>()
  for (const s of sources) {
    map.set(s.ref, {
      ref: s.ref,
      documentId: s.document_id,
      filename: s.filename,
      label: s.label,
      pageNo: null,
      snippet: null,
      weakSupport: false,
      sourceDeleted: false,
      valid: true,
    })
  }
  for (const c of citations) {
    map.set(c.ref, {
      ref: c.ref,
      documentId: c.document_id,
      filename: c.filename,
      label: c.label,
      pageNo: c.page_start,
      snippet: c.snippet,
      weakSupport: c.weak_support,
      sourceDeleted: c.source_deleted,
      valid: true,
    })
  }
  return (ref: string): CitationInfo =>
    map.get(ref) ?? {
      ref,
      documentId: null,
      filename: '',
      label: '',
      pageNo: null,
      snippet: null,
      weakSupport: false,
      sourceDeleted: false,
      valid: false,
    }
}
