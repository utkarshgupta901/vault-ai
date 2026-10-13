import assert from 'node:assert/strict'
import { test } from 'node:test'
import { pageRefLabel } from '../src/lib/documents.ts'

const PDF = 'application/pdf'
const DOCX = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'

test('page chips say "p. N" only for PDFs; sections and parts use the stored label, else a neutral "part N"', () => {
  assert.equal(pageRefLabel(PDF, 4), 'p. 4')
  assert.equal(pageRefLabel(PDF, 4, 'p. iv'), 'p. 4') // labels are not fetched for PDFs; the number is the page
  assert.equal(pageRefLabel(DOCX, 3), 'part 3') // before the label has loaded
  assert.equal(pageRefLabel(DOCX, 3, '§ Termination'), '§ Termination')
  assert.equal(pageRefLabel('text/markdown', 2, '§ Leave (part 2)'), '§ Leave (part 2)')
  assert.equal(pageRefLabel('text/plain', 5, 'part 5'), 'part 5')
})
