import { KeyRound } from 'lucide-react'
import { useState, type FormEvent } from 'react'
import { DEFAULT_API_KEY, getApiKey, setApiKey } from '../api/client'
import { Dialog } from './Dialog'
import { Button } from './ui'

const SEEDED_KEYS = [
  { key: 'vault-demo-key', label: 'demo' },
  { key: 'vault-other-key', label: 'other (tenant isolation)' },
]

/**
 * The API key decides *whose* documents every request sees. Changing it reloads the page so no data from the previous
 * owner can linger in component state.
 */
export function SettingsDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const [value, setValue] = useState(getApiKey)

  function save(e: FormEvent) {
    e.preventDefault()
    setApiKey(value)
    window.location.reload()
  }

  return (
    <Dialog
      open={open}
      onClose={onClose}
      title="Settings"
      description="Requests are authenticated with the X-API-Key header."
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button variant="primary" type="submit" form="settings-form">
            Save and reload
          </Button>
        </>
      }
    >
      <form id="settings-form" onSubmit={save} className="space-y-3">
        <label className="block text-sm font-medium text-zinc-800" htmlFor="api-key">
          API key
        </label>
        <div className="relative">
          <KeyRound className="pointer-events-none absolute top-2.5 left-2.5 size-4 text-zinc-400" aria-hidden />
          <input
            id="api-key"
            value={value}
            onChange={(e) => setValue(e.target.value)}
            autoComplete="off"
            spellCheck={false}
            className="h-9 w-full rounded-md border border-zinc-300 pr-3 pl-8 font-mono text-sm focus:border-accent-500 focus:outline-none"
          />
        </div>
        <p className="text-xs text-zinc-500">Stored in this browser only (localStorage). Default: <code className="font-mono">{DEFAULT_API_KEY}</code></p>
        <div className="flex flex-wrap gap-2">
          {SEEDED_KEYS.map((k) => (
            <button key={k.key} type="button" onClick={() => setValue(k.key)} className="rounded-full border border-zinc-200 px-2.5 py-1 text-xs text-zinc-600 hover:bg-zinc-50">
              Use <span className="font-mono">{k.key}</span> · {k.label}
            </button>
          ))}
        </div>
      </form>
    </Dialog>
  )
}
