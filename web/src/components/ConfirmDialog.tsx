import { useState, type ReactNode } from 'react'
import { Modal } from './Modal'

interface Props {
  title: string
  message: string
  confirmLabel: string
  danger?: boolean
  busy?: boolean
  onConfirm: () => void
  onCancel: () => void
  /**
   * Text the operator must type before the button works. For the handful of
   * actions where a misplaced click is not recoverable by repeating it --
   * taking a host out of a cluster is one -- an "are you sure" answered
   * reflexively is no gate at all.
   */
  confirmText?: string
  /** An extra choice the confirmation itself offers, under the message. */
  children?: ReactNode
}

export function ConfirmDialog({
  title, message, confirmLabel, danger, busy, onConfirm, onCancel, confirmText, children,
}: Props) {
  const [typed, setTyped] = useState('')
  const blocked = confirmText !== undefined && typed.trim() !== confirmText

  return (
    <Modal
      title={title}
      onClose={busy ? () => {} : onCancel}
      footer={
        <>
          <button className="btn" onClick={onCancel} disabled={busy}>Cancel</button>
          <button
            className={`btn ${danger ? 'btn-danger' : 'btn-primary'}`}
            onClick={onConfirm}
            disabled={busy || blocked}
          >
            {busy && <span className="spinner" />}
            {confirmLabel}
          </button>
        </>
      }
    >
      <p style={{ margin: 0, fontSize: 13.5, lineHeight: 1.6 }}>{message}</p>
      {confirmText !== undefined && (
        <div className="field" style={{ marginTop: 14 }}>
          <label htmlFor="confirm-text">
            Type <code>{confirmText}</code> to confirm
          </label>
          <input id="confirm-text" className="input" value={typed} autoFocus
            spellCheck={false} autoCapitalize="off" autoComplete="off" disabled={busy}
            onChange={(event) => setTyped(event.target.value)} />
        </div>
      )}
      {children && <div style={{ marginTop: 14 }}>{children}</div>}
    </Modal>
  )
}
