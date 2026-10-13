/**
 * Server-Sent Events over fetch().
 *
 * Why not EventSource: it cannot send custom headers (our X-API-Key) and only does GET, while the chat stream is a
 * POST with a JSON body. So we read the response body with a ReadableStream and parse the SSE wire format ourselves:
 *
 *   event: token\n
 *   data: {"delta":"Employees receive 18 days [S1]"}\n
 *   \n                                   <- a blank line ends one event
 *   : ping\n\n                           <- a comment (keep-alive), ignored
 *
 * The wire format is parsed by SSEParser (sseParser.ts), which handles events split across network reads.
 */
import { send, type RequestOptions } from './client'
import { SSEParser, type SSEMessage } from './sseParser'

export type { SSEMessage }

/** Reads an SSE response body to the end, calling onMessage for each complete event. */
export async function readEventStream(res: Response, onMessage: (msg: SSEMessage) => void): Promise<void> {
  if (!res.body) return
  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  const parser = new SSEParser()
  try {
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      // {stream: true} keeps a multi-byte UTF-8 character that was split across reads intact.
      for (const msg of parser.push(decoder.decode(value, { stream: true }))) onMessage(msg)
    }
    for (const msg of [...parser.push(decoder.decode()), ...parser.end()]) onMessage(msg)
  } catch (err) {
    // Close the connection if we stop early (abort, or a handler threw); a no-op if it is already closed.
    reader.cancel().catch(() => undefined)
    throw err
  }
}

/**
 * Opens an SSE endpoint (GET or POST) and reads it to the end. Errors *before* the stream starts (401, 404, 409 ...)
 * reject with an ApiError parsed from the JSON envelope, exactly like a normal request. Aborting the signal rejects
 * with an AbortError, which callers treat as "stopped by the user".
 */
export async function openEventStream(
  path: string,
  opts: RequestOptions,
  onMessage: (msg: SSEMessage) => void,
  onOpen?: (res: Response) => void,
): Promise<void> {
  const res = await send(path, { ...opts, accept: 'text/event-stream' })
  onOpen?.(res)
  await readEventStream(res, onMessage)
}

/** Parses an event's JSON data. Unknown fields are kept but ignored by the typed consumer. */
export function parseData<T>(msg: SSEMessage): T {
  return JSON.parse(msg.data) as T
}
