import assert from 'node:assert/strict'
import { test } from 'node:test'
import { ReconnectBackoff, reconnectDelay, stopsFollowing } from '../src/lib/reconnect.ts'

test('the wait doubles from 3 s per consecutive early end and is capped at 60 s', () => {
  const waits = [1, 2, 3, 4, 5, 6, 7, 20, 1_000].map(reconnectDelay)
  assert.deepEqual(waits, [3_000, 6_000, 12_000, 24_000, 48_000, 60_000, 60_000, 60_000, 60_000])
})

test('a status event resets the back-off; each document backs off on its own', () => {
  const backoff = new ReconnectBackoff()
  assert.deepEqual([backoff.ended('a', null), backoff.ended('a', 0), backoff.ended('a', 503)], [3_000, 6_000, 12_000])
  assert.equal(backoff.ended('b', null), 3_000)
  backoff.connected('a') // the reconnect worked: an event arrived
  assert.equal(backoff.ended('a', null), 3_000)
  assert.equal(backoff.ended('b', null), 6_000)
})

test('401, 403 and 404 stop following the document; network errors, 429 and 5xx keep retrying', () => {
  for (const status of [401, 403, 404]) {
    assert.equal(stopsFollowing(status), true, String(status))
    const backoff = new ReconnectBackoff()
    assert.equal(backoff.ended('a', status), null, String(status))
    assert.equal(backoff.isStopped('a'), true)
    assert.equal(backoff.isStopped('b'), false)
  }
  for (const status of [null, 0, 409, 429, 500, 502, 503]) {
    assert.equal(stopsFollowing(status), false, String(status))
    const backoff = new ReconnectBackoff()
    assert.equal(typeof backoff.ended('a', status), 'number', String(status))
    assert.equal(backoff.isStopped('a'), false)
  }
})
