import type { ReactNode } from 'react'
import { Modal } from './Modal'

interface Props {
  title: string
  message: string
  confirmLabel: string
  danger?: boolean
  busy?: boolean
  onConfirm: () => void
  onCancel: () => void
  /** An extra choice the confirmation itself offers, under the message. */
  children?: ReactNode
}

export function ConfirmDialog({
  title, message, confirmLabel, danger, busy, onConfirm, onCancel, children,
}: Props) {
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
            disabled={busy}
          >
            {busy && <span className="spinner" />}
            {confirmLabel}
          </button>
        </>
      }
    >
      <p style={{ margin: 0, fontSize: 13.5, lineHeight: 1.6 }}>{message}</p>
      {children && <div style={{ marginTop: 14 }}>{children}</div>}
    </Modal>
  )
}
