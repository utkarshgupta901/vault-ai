import assert from 'node:assert/strict'
import { test } from 'node:test'
import { isUnverified, rewriteDiffers, warningCodes } from '../src/lib/answers.ts'
import { fmtUsd } from '../src/lib/format.ts'

test('"Interpreted as" only when the rewrite changed the question', () => {
  assert.equal(rewriteDiffers('How many PTO days?', 'how many PTO days'), false)
  assert.equal(rewriteDiffers('And after three years?', 'How many PTO days after three years of service?'), true)
  assert.equal(rewriteDiffers('Q', null), false)
})

test('unverified when ungrounded or any invalid citation', () => {
  assert.equal(isUnverified({ answerType: 'answered', signals: { invalid_citations: 0 } }), false)
  assert.equal(isUnverified({ answerType: 'ungrounded', signals: null }), true)
  assert.equal(isUnverified({ answerType: 'answered', signals: { invalid_citations: 2 } }), true)
})

test('warnings accept plain codes and {code} objects', () => {
  assert.deepEqual(warningCodes(['keyword_only', { code: 'fallback_model', detail: 'x' }]), ['keyword_only', 'fallback_model'])
})

test('money accepts Pydantic Decimal strings', () => {
  assert.equal(fmtUsd('0.001234'), '$0.0012')
  assert.equal(fmtUsd(0), '$0')
  assert.equal(fmtUsd(null), '—')
  assert.equal(fmtUsd(1.5), '$1.50')
})
