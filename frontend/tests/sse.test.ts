// Run with `pnpm test` (node's built-in test runner; node strips the TypeScript types, no extra dependencies).
import assert from 'node:assert/strict'
import { test } from 'node:test'
import { SSEParser, type SSEMessage } from '../src/api/sseParser.ts'

const WIRE =
  'event: meta\ndata: {"a":1}\n\n' +
  ': ping\n\n' +
  'event: token\ndata: {"delta":"Hi [S"}\n\n' +
  'event: token\r\ndata: {"delta":"1] ü"}\r\n\r\n' +
  'event: unknown_future\ndata: {}\n\n' +
  'data: multi\ndata: line\n\n' +
  'event: done\ndata: {"ok":true}'

function parse(chunks: string[]): SSEMessage[] {
  const parser = new SSEParser()
  const out: SSEMessage[] = []
  for (const chunk of chunks) out.push(...parser.push(chunk))
  out.push(...parser.end())
  return out
}

test('parses events, skips comments, joins multi-line data, keeps unknown names for the caller to ignore', () => {
  assert.deepEqual(parse([WIRE]), [
    { event: 'meta', data: '{"a":1}' },
    { event: 'token', data: '{"delta":"Hi [S"}' },
    { event: 'token', data: '{"delta":"1] ü"}' },
    { event: 'unknown_future', data: '{}' },
    { event: 'message', data: 'multi\nline' },
    { event: 'done', data: '{"ok":true}' },
  ])
})

test('gives identical results however the stream is split across reads (incl. a split \\r\\n)', () => {
  const whole = parse([WIRE])
  for (let i = 0; i <= WIRE.length; i++) assert.deepEqual(parse([WIRE.slice(0, i), WIRE.slice(i)]), whole, `split at ${i}`)
  assert.deepEqual(parse([...WIRE]), whole)
})

test('emits nothing until the blank line that ends an event arrives', () => {
  const parser = new SSEParser()
  assert.deepEqual(parser.push('event: token\ndata: {"delta":"x"}\n'), [])
  assert.deepEqual(parser.push('\n'), [{ event: 'token', data: '{"delta":"x"}' }])
})
