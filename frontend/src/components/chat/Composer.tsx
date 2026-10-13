import { ArrowUp, Square } from 'lucide-react'
import { useState, type FormEvent, type KeyboardEvent } from 'react'
import { cx } from '../../lib/cx'

const MAX_CHARS = 2000 // MessageCreate.content max_length

interface ComposerProps {
  busy: boolean
  disabled?: boolean
  stream: boolean
  onStreamChange: (stream: boolean) => void
  onSend: (text: string) => void
  onStop: () => void
}

/** Question box: Enter sends, Shift+Enter adds a line. While an answer streams the send button becomes Stop. */
export function Composer({ busy, disabled, stream, onStreamChange, onSend, onStop }: ComposerProps) {
  const [text, setText] = useState('')

  function submit(e?: FormEvent) {
    e?.preventDefault()
    const value = text.trim()
    if (!value || busy || disabled) return
    onSend(value)
    setText('')
  }

  function onKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault()
      submit()
    }
  }

  return (
    <form onSubmit={submit} className="border-t border-zinc-200 bg-white px-4 py-3">
      <div className="flex items-end gap-2 rounded-xl border border-zinc-300 bg-white px-3 py-2 focus-within:border-accent-500 focus-within:ring-2 focus-within:ring-accent-100">
        <textarea
          value={text}
          onChange={(e) => setText(e.target.value.slice(0, MAX_CHARS))}
          onKeyDown={onKeyDown}
          rows={Math.min(6, Math.max(1, text.split('\n').length))}
          placeholder={disabled ? 'Select or start a chat first' : 'Ask a question about your documents…'}
          disabled={disabled}
          aria-label="Question"
          className="max-h-40 min-h-6 flex-1 resize-none bg-transparent text-[0.9375rem] leading-6 text-zinc-900 placeholder:text-zinc-400 focus:outline-none"
        />
        {busy ? (
          <button type="button" onClick={onStop} className="grid size-8 shrink-0 place-items-center rounded-lg bg-zinc-900 text-white hover:bg-zinc-700" aria-label="Stop generating">
            <Square className="size-3.5 fill-current" />
          </button>
        ) : (
          <button
            type="submit"
            disabled={!text.trim() || disabled}
            className="grid size-8 shrink-0 place-items-center rounded-lg bg-accent-600 text-white hover:bg-accent-700 disabled:bg-zinc-200 disabled:text-zinc-400"
            aria-label="Send"
          >
            <ArrowUp className="size-4" />
          </button>
        )}
      </div>
      <div className="mt-1.5 flex items-center justify-between text-xs text-zinc-400">
        <label className="flex cursor-pointer items-center gap-2 select-none" title="Streaming uses POST .../messages/stream (SSE); off uses POST .../messages (one JSON response).">
          <span className={cx('relative inline-flex h-4 w-7 items-center rounded-full transition-colors has-focus-visible:ring-2 has-focus-visible:ring-accent-500 has-focus-visible:ring-offset-1', stream ? 'bg-accent-600' : 'bg-zinc-300')}>
            <input type="checkbox" checked={stream} onChange={(e) => onStreamChange(e.target.checked)} className="peer sr-only" />
            <span className={cx('inline-block size-3 rounded-full bg-white shadow transition-transform', stream ? 'translate-x-3.5' : 'translate-x-0.5')} />
          </span>
          <span className="text-zinc-500">{stream ? 'Streaming' : 'Non-streaming (JSON)'}</span>
        </label>
        <span className="tabular-nums">{text.length > MAX_CHARS * 0.8 ? `${text.length}/${MAX_CHARS}` : 'Enter to send · Shift+Enter for a new line'}</span>
      </div>
    </form>
  )
}
