import type { SessionOut } from '../api/types'

/** Human label for a session: its title, else a description of its scope. */
export function sessionLabel(s: SessionOut, docNames: Map<string, string>): string {
  if (s.title) return s.title
  if (s.all_documents) return 'All documents'
  if (s.document_ids.length === 1) return docNames.get(s.document_ids[0]) ?? '1 document'
  return `${s.document_ids.length} documents`
}
