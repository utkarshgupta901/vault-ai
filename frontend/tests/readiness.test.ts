import assert from 'node:assert/strict'
import { afterEach, test } from 'node:test'
import { isAbortError } from '../src/api/client.ts'
import { fetchReadiness } from '../src/api/readiness.ts'

const realFetch = globalThis.fetch
afterEach(() => {
  globalThis.fetch = realFetch
})

/** A server that accepts the request and never answers, like a wedged API: settles only when the signal aborts. */
function hangingFetch(_url: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  return new Promise((_resolve, reject) => init?.signal?.addEventListener('abort', () => reject(init.signal?.reason)))
}

const respond = (body: string, status = 200) => async () => new Response(body, { status })

const READY = { status: 'ok', checks: { database: { ok: true, ms: 3 } } }

test('a hung probe resolves to null ("unavailable") after the timeout instead of never settling', async () => {
  globalThis.fetch = hangingFetch
  const started = Date.now()
  assert.equal(await fetchReadiness({}, new AbortController().signal, 30), null)
  assert.ok(Date.now() - started < 2_000)
})

test('a caller abort is rethrown as an AbortError, so useApi ignores it instead of flashing "unavailable"', async () => {
  globalThis.fetch = hangingFetch
  const controller = new AbortController()
  const pending = fetchReadiness({}, controller.signal, 5_000)
  controller.abort()
  await assert.rejects(pending, (err) => isAbortError(err))
})

test('reads the JSON body for any status, including 503 from a failed hard dependency', async () => {
  globalThis.fetch = respond(JSON.stringify(READY))
  assert.deepEqual(await fetchReadiness({}), READY)
  const down = { status: 'unavailable', checks: { database: { ok: false, error: 'connection refused' } } }
  globalThis.fetch = respond(JSON.stringify(down), 503)
  assert.deepEqual(await fetchReadiness({}), down)
})

test('network errors, non-JSON bodies and bodies that are not a readiness report all mean "unreachable"', async () => {
  globalThis.fetch = async () => {
    throw new TypeError('Failed to fetch')
  }
  assert.equal(await fetchReadiness({}), null)
  for (const body of ['<html>502 Bad Gateway</html>', '{"error":{"code":"NOT_FOUND"}}', '{"status":"weird","checks":{}}', 'null']) {
    globalThis.fetch = respond(body, 502)
    assert.equal(await fetchReadiness({}), null, body)
  }
})
