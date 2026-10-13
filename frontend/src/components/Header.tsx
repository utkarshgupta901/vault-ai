import { BookOpen, FileText, Gauge, MessagesSquare, Settings, ShieldCheck } from 'lucide-react'
import { useState } from 'react'
import { NavLink } from 'react-router'
import { cx } from '../lib/cx'
import { HealthIndicator } from './HealthIndicator'
import { SettingsDialog } from './SettingsDialog'

const NAV = [
  { to: '/', label: 'Documents', icon: FileText, end: true },
  { to: '/chat', label: 'Chat', icon: MessagesSquare, end: false },
  { to: '/metrics', label: 'Metrics', icon: Gauge, end: false },
]

export function Header() {
  const [settingsOpen, setSettingsOpen] = useState(false)
  return (
    <header className="sticky top-0 z-30 border-b border-zinc-200 bg-white/90 backdrop-blur">
      <div className="mx-auto flex h-14 max-w-[1400px] items-center gap-6 px-4 lg:px-6">
        <NavLink to="/" className="flex items-center gap-2 font-semibold tracking-tight text-zinc-900">
          <span className="grid size-7 place-items-center rounded-md bg-blue-900 text-white">
            <ShieldCheck className="size-4" aria-hidden />
          </span>
          Vault AI
        </NavLink>
        <nav className="flex items-center gap-1" aria-label="Main">
          {NAV.map(({ to, label, icon: Icon, end }) => (
            <NavLink
              key={to}
              to={to}
              end={end}
              className={({ isActive }) =>
                cx(
                  'flex items-center gap-1.5 rounded-md px-3 py-1.5 text-sm font-medium transition-colors',
                  isActive ? 'bg-zinc-100 text-zinc-900' : 'text-zinc-500 hover:bg-zinc-50 hover:text-zinc-900',
                )
              }
            >
              <Icon className="size-4" aria-hidden />
              {label}
            </NavLink>
          ))}
        </nav>
        <div className="ml-auto flex items-center gap-1">
          <HealthIndicator />
          <a href="/docs" target="_blank" rel="noreferrer" className="flex items-center gap-1.5 rounded-md px-2 py-1 text-xs text-zinc-600 hover:bg-zinc-100" title="Swagger / OpenAPI docs">
            <BookOpen className="size-4" aria-hidden />
            <span className="hidden md:inline">API docs</span>
          </a>
          <button type="button" onClick={() => setSettingsOpen(true)} className="flex items-center gap-1.5 rounded-md px-2 py-1 text-xs text-zinc-600 hover:bg-zinc-100" aria-label="Settings (API key)">
            <Settings className="size-4" aria-hidden />
            <span className="hidden md:inline">Settings</span>
          </button>
        </div>
      </div>
      {settingsOpen && <SettingsDialog open onClose={() => setSettingsOpen(false)} />}
    </header>
  )
}
