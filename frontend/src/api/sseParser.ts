/**
 * Incremental parser for the Server-Sent Events wire format (no imports, so it is unit-tested directly by node).
 *
 *   event: token\n
 *   data: {"delta":"Employees receive 18 days [S1]"}\n
 *   \n                                   <- a blank line ends one event
 *   : ping\n\n                           <- a comment (keep-alive), ignored
 *
 * Network reads do not respect event boundaries: one read can hold half an event or three events, so the parser keeps
 * a buffer and only emits an event once its terminating blank line has arrived.
 */
export interface SSEMessage {
  event: string
  data: string
}

const LINE_END = /\r\n|\r|\n/

/** Incremental SSE parser: push() decoded text chunks of any size, get back the events they complete. */
export class SSEParser {
  private buffer = ''
  private eventName = ''
  private dataLines: string[] = []

  push(chunk: string): SSEMessage[] {
    this.buffer += chunk
    const out: SSEMessage[] = []
    for (let m = LINE_END.exec(this.buffer); m !== null; m = LINE_END.exec(this.buffer)) {
      // A lone '\r' at the very end may be the first half of a '\r\n' split across two reads: wait for more input.
      if (m[0] === '\r' && m.index === this.buffer.length - 1) break
      const line = this.buffer.slice(0, m.index)
      this.buffer = this.buffer.slice(m.index + m[0].length)
      const msg = this.processLine(line)
      if (msg) out.push(msg)
    }
    return out
  }

  /** End of stream. Lenient: dispatch a last event even if the server closed without the final blank line. */
  end(): SSEMessage[] {
    const out: SSEMessage[] = []
    if (this.buffer) {
      const msg = this.processLine(this.buffer)
      this.buffer = ''
      if (msg) out.push(msg)
    }
    const last = this.dispatch()
    if (last) out.push(last)
    return out
  }

  private processLine(line: string): SSEMessage | null {
    if (line === '') return this.dispatch()
    if (line.startsWith(':')) return null // comment / keep-alive such as ": ping"
    const colon = line.indexOf(':')
    const field = colon === -1 ? line : line.slice(0, colon)
    let value = colon === -1 ? '' : line.slice(colon + 1)
    if (value.startsWith(' ')) value = value.slice(1)
    if (field === 'event') this.eventName = value
    else if (field === 'data') this.dataLines.push(value)
    // `id` and `retry` are only meaningful for EventSource auto-reconnect, which we do not use.
    return null
  }

  private dispatch(): SSEMessage | null {
    const msg = this.dataLines.length ? { event: this.eventName || 'message', data: this.dataLines.join('\n') } : null
    this.eventName = ''
    this.dataLines = []
    return msg
  }
}
