import { useState, type ReactNode } from 'react'
import { Dialog } from './Dialog'
import { Button } from './ui'

interface ConfirmDialogProps {
  open: boolean
  title: string
  children: ReactNode
  confirmLabel: string
  onConfirm: () => Promise<void> | void
  onClose: () => void
}

/** A destructive-action confirmation that stays open (with a busy button) until the action finishes. */
export function ConfirmDialog({ open, title, children, confirmLabel, onConfirm, onClose }: ConfirmDialogProps) {
  const [busy, setBusy] = useState(false)

  async function confirm() {
    setBusy(true)
    try {
      await onConfirm()
    } finally {
      setBusy(false)
    }
  }

  return (
    <Dialog
      open={open}
      onClose={onClose}
      title={title}
      footer={
        <>
          <Button onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button variant="danger" busy={busy} onClick={confirm}>
            {confirmLabel}
          </Button>
        </>
      }
    >
      <div className="text-sm text-zinc-600">{children}</div>
    </Dialog>
  )
}
