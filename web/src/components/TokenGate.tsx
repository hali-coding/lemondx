import { useState } from 'react'
import { setToken } from '../lib/api'
import { Modal } from './Modal'

/** Shown when the API answers 401 — the server was started with --token. */
export function TokenGate({ onSubmit }: { onSubmit: () => void }) {
  const [value, setValue] = useState('')

  function submit(event: React.FormEvent) {
    event.preventDefault()
    if (!value.trim()) return
    setToken(value.trim())
    onSubmit()
  }

  return (
    <Modal
      title="API token required"
      subtitle="This server was started with --token. Paste it to continue."
      onClose={() => {}}
      footer={
        <button type="submit" form="token-form" className="btn btn-primary" disabled={!value.trim()}>
          Unlock
        </button>
      }
    >
      <form id="token-form" onSubmit={submit} className="field">
        <label htmlFor="token-input">Token</label>
        <input
          id="token-input"
          className="input mono"
          type="password"
          value={value}
          onChange={(event) => setValue(event.target.value)}
          autoComplete="off"
          spellCheck={false}
        />
        <span className="hint">Kept for this browser tab only.</span>
      </form>
    </Modal>
  )
}
