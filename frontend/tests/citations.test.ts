import assert from 'node:assert/strict'
import { test } from 'node:test'
import { buildCitationLookup, linkifyCitations, splitIncompleteMarker } from '../src/lib/citations.ts'

test('holds back an incomplete citation marker at the end of streamed text', () => {
  for (const tail of ['[', '[S', '[S1', '[S12', '[S1,', '[S1, ', '[S1, S', '[S1, S2']) {
    assert.deepEqual(splitIncompleteMarker(`18 days ${tail}`), { stable: '18 days ', pending: tail }, tail)
  }
})

test('releases complete markers and ordinary brackets', () => {
  for (const text of ['18 days [S1]', 'see [x', 'plain text', 'a [S1] b']) {
    assert.deepEqual(splitIncompleteMarker(text), { stable: text, pending: '' }, text)
  }
})

test('rewrites [S1] and [S1, S3] markers into fragment links for the chip renderer', () => {
  assert.equal(linkifyCitations('18 days [S1][S2] and [S1, S3].'), '18 days [S1](#cite-S1)[S2](#cite-S2) and [S1](#cite-S1)[S3](#cite-S3).')
  assert.equal(linkifyCitations('no markers [1] [Sx]'), 'no markers [1] [Sx]')
})

test('resolves refs from citations first, then sources, else marks them invalid', () => {
  const docId = '00000000-0000-0000-0000-000000000001'
  const lookup = buildCitationLookup(
    [{ ref: 'S1', chunk_id: 1, document_id: docId, filename: 'a.pdf', label: 'p. 4', page_start: 4, page_end: 4, snippet: '18 days', weak_support: false, source_deleted: false }],
    [{ ref: 'S2', chunk_id: 2, document_id: docId, filename: 'a.pdf', label: 'p. 5' }],
  )
  assert.equal(lookup('S1').pageNo, 4)
  assert.equal(lookup('S1').snippet, '18 days')
  assert.equal(lookup('S2').pageNo, null)
  assert.equal(lookup('S2').valid, true)
  assert.equal(lookup('S9').valid, false)
})
