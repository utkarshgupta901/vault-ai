import assert from 'node:assert/strict'
import { test } from 'node:test'
import { buildCitationLookup, linkifyCitations, splitIncompleteMarker } from '../src/lib/citations.ts'

test('holds back an incomplete citation marker at the end of streamed text', () => {
  for (const tail of ['[', '[S', '[S1', '[S12', '[S1,', '[S1, ', '[S1, S', '[S1, S2']) {
    assert.deepEqual(splitIncompleteMarker(`18 days ${tail}`), { stable: '18 days ', pending: tail }, tail)
  }
})

test('holds back the incomplete tail of every marker form the backend accepts', () => {
  for (const tail of ['[s', '[S1-', '[S1 -', '[S1–S', '[S1-3', '[S1; ', '[S1, 2', '[s2\u2011', '[S1 ']) {
    assert.deepEqual(splitIncompleteMarker(`18 days ${tail}`), { stable: '18 days ', pending: tail }, tail)
  }
})

test('releases complete markers and ordinary brackets', () => {
  for (const text of ['18 days [S1]', 'see [x', 'plain text', 'a [S1] b', '18 days [S1-3]', 'see [Sx', '[S1 and']) {
    assert.deepEqual(splitIncompleteMarker(text), { stable: text, pending: '' }, text)
  }
})

test('rewrites [S1] and [S1, S3] markers into fragment links for the chip renderer', () => {
  assert.equal(linkifyCitations('18 days [S1][S2] and [S1, S3].'), '18 days [S1](#cite-S1)[S2](#cite-S2) and [S1](#cite-S1)[S3](#cite-S3).')
  assert.equal(linkifyCitations('no markers [1] [Sx]'), 'no markers [1] [Sx]')
})

const chips = (...refs: string[]) => refs.map((ref) => `[${ref}](#cite-${ref})`).join('')

test('rewrites ranges, lower case, semicolons and bare numbers like the backend counts them', () => {
  const cases: [string, string][] = [
    ['18 days [S1-3].', `18 days ${chips('S1', 'S2', 'S3')}.`],
    ['18 days [S1–S3].', `18 days ${chips('S1', 'S2', 'S3')}.`],
    ['18 days [S2 — 4].', `18 days ${chips('S2', 'S3', 'S4')}.`],
    ['18 days [s1].', `18 days ${chips('S1')}.`],
    ['18 days [S1; S2].', `18 days ${chips('S1', 'S2')}.`],
    ['18 days [S1, 2].', `18 days ${chips('S1', 'S2')}.`],
    ['18 days [S1, S3-4; 6].', `18 days ${chips('S1', 'S3', 'S4', 'S6')}.`],
    ['18 days [S2-2].', `18 days ${chips('S2')}.`],
  ]
  for (const [text, linked] of cases) assert.equal(linkifyCitations(text), linked, text)
})

test('ranges written with any hyphen or minus are chips too', () => {
  for (const dash of ['\u2010', '\u2011', '\u2212']) {
    assert.equal(linkifyCitations(`18 days [S1${dash}2].`), `18 days ${chips('S1', 'S2')}.`, dash)
  }
})

test('an absurd or reversed range keeps only its two ends, as the backend counts it', () => {
  assert.equal(linkifyCitations('[S1-999]'), chips('S1', 'S999'))
  assert.equal(linkifyCitations('[S3-S1]'), chips('S3', 'S1'))
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
