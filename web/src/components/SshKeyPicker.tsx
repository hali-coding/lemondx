import { useState } from 'react'
import { api } from '../lib/api'
import type { SshKey } from '../lib/types'

interface Props {
  hostKeys: SshKey[]
  /** Selected keys, as full `type base64 comment` lines. */
  value: string[]
  onChange: (next: string[]) => void
  disabled?: boolean
}

/**
 * Choose which public keys get installed. Shown at the top level of the create
 * dialog (not tucked inside the bootstrap section) whenever a selected module
 * installs keys, since it is a decision worth seeing without hunting for it.
 */
export function SshKeyPicker({ hostKeys, value, onChange, disabled }: Props) {
  const [pasted, setPasted] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [extraKeys, setExtraKeys] = useState<SshKey[]>([])

  const known = [...hostKeys, ...extraKeys]
  // Keys that came from a saved profile or template need not be in this
  // host's ~/.ssh; without a row of their own they would be selected unseen.
  const saved: SshKey[] = value
    .filter((line) => !known.some((k) => k.line === line))
    .map((line) => {
      const [type, , ...comment] = line.split(' ')
      return { type, comment: comment.join(' '), fingerprint: '', line, source: undefined }
    })
  const allKeys = [...known, ...saved]

  function toggle(line: string) {
    onChange(value.includes(line) ? value.filter((k) => k !== line) : [...value, line])
  }

  async function addPasted() {
    const text = pasted.trim()
    if (!text) return
    try {
      // The server is the authority on what counts as an acceptable key.
      const key = await api.validateSshKey(text)
      if (!allKeys.some((k) => k.line === key.line)) setExtraKeys((keys) => [...keys, key])
      onChange([...new Set([...value, key.line])])
      setPasted('')
      setError(null)
    } catch (cause) {
      setError((cause as Error).message)
    }
  }

  return (
    <div className="field ssh-keys">
      <label>
        SSH public keys
        {value.length > 0 && (
          <span className="badge badge-ok">{value.length} selected</span>
        )}
      </label>

      {allKeys.length === 0 ? (
        <span className="hint">
          No keys found in <span className="mono">~/.ssh</span>. Paste one below.
        </span>
      ) : (
        allKeys.map((key) => (
          <label className="checkbox key-row" key={key.line}>
            <input
              type="checkbox"
              checked={value.includes(key.line)}
              disabled={disabled}
              onChange={() => toggle(key.line)}
            />
            <span className="list-row-main">
              <strong>{key.source || key.comment || key.type}</strong>{' '}
              <span className="faint">{key.type}</span>
              {key.source && key.comment && (
                <span className="faint"> · {key.comment}</span>
              )}
              {key.fingerprint ? (
                <span className="mono faint key-fp">{key.fingerprint}</span>
              ) : (
                <span className="faint key-fp">saved key, not in ~/.ssh</span>
              )}
            </span>
          </label>
        ))
      )}

      <div className="console-form" style={{ marginTop: 6 }}>
        <input
          className="input mono"
          value={pasted}
          placeholder="ssh-ed25519 AAAA… you@host"
          aria-label="Paste a public key"
          autoComplete="off"
          spellCheck={false}
          disabled={disabled}
          onChange={(event) => setPasted(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === 'Enter') {
              event.preventDefault()
              addPasted()
            }
          }}
        />
        <button type="button" className="btn" onClick={addPasted}
          disabled={disabled || !pasted.trim()}>
          Add
        </button>
      </div>

      {error && <span className="hint" style={{ color: 'var(--danger)' }}>{error}</span>}
      <span className="hint">Public keys only — never paste a private key.</span>
    </div>
  )
}
