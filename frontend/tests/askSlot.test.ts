import assert from 'node:assert/strict'
import { test } from 'node:test'
import { askExclusively, type AskSlot, type AskSteps } from '../src/lib/askSlot.ts'

/** A history refresh (loadLatest) that is still in flight until the test settles it. Like listMessages it honours the
 *  signal: an abort rejects it and its response is never written. */
function pendingRefresh(write: () => void) {
  let settle = () => {}
  const refresh = (signal?: AbortSignal) =>
    new Promise<void>((resolve, reject) => {
      signal?.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')))
      settle = () => {
        if (!signal?.aborted) write()
        resolve()
      }
    })
  return { refresh, settle: () => settle() }
}

test('two rapid asks after a stopped turn send exactly one request', async () => {
  const slot: AskSlot = { current: null }
  const requests: AbortSignal[] = []
  const steps = (): AskSteps => ({
    refresh: () => new Promise((resolve) => setTimeout(resolve, 5)),
    isCurrent: () => true,
    send: async (signal) => {
      requests.push(signal)
    },
  })
  const outcomes = await Promise.all([askExclusively(slot, steps()), askExclusively(slot, steps())])
  assert.equal(requests.length, 1)
  assert.deepEqual(outcomes, ['sent', 'busy'])
  assert.equal(slot.current, null, 'the slot is free again once the answer is in')
})

test('Stop reaches the question in flight, and the slot frees up after a stopped or failed answer', async () => {
  const slot: AskSlot = { current: null }
  const stopped = askExclusively(slot, {
    refresh: null,
    isCurrent: () => true,
    send: (signal) => new Promise((_, reject) => signal.addEventListener('abort', () => reject(signal.reason))),
  })
  slot.current?.abort() // the Stop button
  await assert.rejects(stopped)
  assert.equal(slot.current, null)
  const failed = askExclusively(slot, { refresh: null, isCurrent: () => true, send: () => Promise.reject(new Error('502')) })
  await assert.rejects(failed, /502/)
  assert.equal(await askExclusively(slot, { refresh: null, isCurrent: () => true, send: async () => {} }), 'sent')
})

test('switching session during the history refresh: no stale write, no request, no live turn', async () => {
  const view = { session: 'A', messages: ['a1'], live: null as string | null, requests: [] as string[] }
  const slot: AskSlot = { current: null }
  const { refresh, settle } = pendingRefresh(() => {
    view.messages = ['a1', 'a2']
  })
  const pending = askExclusively(slot, {
    refresh,
    isCurrent: () => view.session === 'A',
    send: async () => {
      view.live = 'question for A'
      view.requests.push('A')
    },
  })
  // The user opens chat B: the render resets the view and the effect cleanup aborts the slot (useChat).
  view.session = 'B'
  view.messages = []
  slot.current?.abort()
  settle()
  assert.equal(await pending, 'cancelled')
  assert.deepEqual(view, { session: 'B', messages: [], live: null, requests: [] })
  assert.equal(slot.current, null)
})

test('a session switch seen only after the refresh (before the cleanup ran) still cancels the question', async () => {
  let session = 'A'
  const requests: string[] = []
  const outcome = await askExclusively(
    { current: null },
    {
      refresh: async () => {
        session = 'B'
      },
      isCurrent: () => session === 'A',
      send: async () => {
        requests.push('A')
      },
    },
  )
  assert.equal(outcome, 'cancelled')
  assert.deepEqual(requests, [])
})

test('a failed refresh does not block the question; a first question or a retry skips the refresh', async () => {
  const sent: string[] = []
  const send = async () => {
    sent.push('q')
  }
  const failing = { refresh: () => Promise.reject(new Error('NETWORK_ERROR')), isCurrent: () => true, send }
  assert.equal(await askExclusively({ current: null }, failing), 'sent')
  const slot: AskSlot = { current: null }
  const first = askExclusively(slot, { refresh: null, isCurrent: () => true, send })
  assert.ok(slot.current, 'claimed synchronously, before the first await')
  assert.equal(await first, 'sent')
  assert.equal(sent.length, 2)
})
