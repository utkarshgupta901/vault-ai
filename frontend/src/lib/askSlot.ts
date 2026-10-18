/**
 * The one question a chat view may have in flight (useChat's ask), kept free of React so `pnpm test` can check the
 * ordering under node.
 *
 * ask() used to check "already asking?" before its first await (re-reading the history after a stopped or failed
 * turn) but claim the slot only after it. A double click, or a follow-up chip clicked twice, in that gap passed the
 * check twice: two POSTs with different client_message_ids, so the server generated and billed two answers, and Stop
 * could abort only the later one. Switching chat in that gap also let the old question start inside the new chat.
 */

/** The hook's useRef: the controller of the question in flight, or null. Leaving the session aborts it. */
export interface AskSlot {
  current: AbortController | null
}

export interface AskSteps {
  /** Re-reads the history before a new question (null: nothing to fold in). Must stop when `signal` aborts. */
  refresh: ((signal: AbortSignal) => Promise<void>) | null
  /** False once the view shows another session: the old question must not start in the new one's state. */
  isCurrent: () => boolean
  /** Shows the live turn and sends the question; `signal` is what Stop aborts. */
  send: (signal: AbortSignal) => Promise<void>
}

/** 'busy': another question holds the slot; 'cancelled': aborted (Stop, session switch) before it was sent. */
export type AskOutcome = 'sent' | 'busy' | 'cancelled'

/** Runs one ask while holding the slot. The slot is claimed synchronously, before the first await, so a second call
 *  made in the meantime sees 'busy' instead of sending its own request. */
export async function askExclusively(slot: AskSlot, steps: AskSteps): Promise<AskOutcome> {
  if (slot.current) return 'busy'
  const controller = new AbortController()
  slot.current = controller
  try {
    // A failed refresh only leaves an older view of the history; it must not block the question.
    if (steps.refresh) await steps.refresh(controller.signal).catch(() => undefined)
    // The session-switch cleanup aborts the slot; isCurrent also covers a switch it has not reached yet.
    if (controller.signal.aborted || !steps.isCurrent()) return 'cancelled'
    await steps.send(controller.signal)
    return 'sent'
  } finally {
    if (slot.current === controller) slot.current = null
  }
}
