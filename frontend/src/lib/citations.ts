/**
 * Citation markers in answers look like [S1] or [S1, S3]; each Sn points at one source chunk sent to the model. Models
 * also write ranges ([S1-3], [S1–S3]), lower case ([s1]), semicolons and bare numbers ([S1; 2]). The grammar mirrors
 * CITATION_GROUP_RE in app/rag/citations.py: whatever the backend counts as a citation (and vouches for in the
 * confidence badge) must render as a clickable chip, not as plain text.
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

// Range dashes: hyphen-minus, U+2010 hyphen, U+2011 non-breaking hyphen, en dash, em dash, U+2212 minus.
const DASHES = '\\-\\u2010\\u2011\\u2013\\u2014\\u2212'
const RANGE_DASH = `\\s*[${DASHES}]\\s*`
const REF_ITEM = `S?\\d+(?:${RANGE_DASH}S?\\d+)?`
const MARKER = new RegExp(`\\[(S\\d+(?:${RANGE_DASH}S?\\d+)?(?:\\s*[,;]\\s*${REF_ITEM})*)\\]`, 'gi')
const REF_ITEM_RE = new RegExp(`(\\d+)(?:${RANGE_DASH}S?(\\d+))?`, 'gi')
// "[" or "[S" followed only by characters a marker can contain. Looser than MARKER on purpose: the tail is held back
// only until the next delta, which either completes it into a marker or shows it was ordinary text.
const INCOMPLETE_TAIL = new RegExp(`\\[(?:S[\\dS\\s,;${DASHES}]*)?$`, 'i')
const MAX_REF_RANGE = 20 // as in the backend: [S1-20] expands, a wider or reversed range keeps only its two ends

/** Splits streamed text into the part that is safe to render now and a pending incomplete marker. */
export function splitIncompleteMarker(text: string): { stable: string; pending: string } {
  const m = INCOMPLETE_TAIL.exec(text)
  if (!m) return { stable: text, pending: '' }
  return { stable: text.slice(0, m.index), pending: m[0] }
}

/** A range's ids with the backend's cap (app/rag/citations.py _expand), so chip validity matches invalid_citations. */
function expand(first: number, last: number | null): number[] {
  if (last === null) return [first]
  if (first <= last && last < first + MAX_REF_RANGE) return Array.from({ length: last - first + 1 }, (_, i) => first + i)
  return [first, last]
}

/** The refs of one marker's inside, normalised to "S<n>": "s1-3; 6" -> S1, S2, S3, S6. */
function markerRefs(inside: string): string[] {
  return [...inside.matchAll(REF_ITEM_RE)].flatMap((m) =>
    expand(Number(m[1]), m[2] === undefined ? null : Number(m[2])).map((n) => `S${n}`),
  )
}

export function linkifyCitations(text: string): string {
  return text.replace(MARKER, (_, inside: string) =>
    markerRefs(inside)
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
