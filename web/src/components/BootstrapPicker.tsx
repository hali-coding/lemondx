import { useMemo, useState } from 'react'
import { api } from '../lib/api'
import type { BootstrapModule, BootstrapSelection, SshKey } from '../lib/types'

interface Props {
  modules: BootstrapModule[]
  hostKeys: SshKey[]
  value: BootstrapSelection
  onChange: (next: BootstrapSelection) => void
  disabled?: boolean
}

/**
 * Module checkboxes, their parameters, and SSH key selection. Shared by the
 * create dialog and the drawer's Bootstrap tab.
 */
export function BootstrapPicker({ modules, hostKeys, value, onChange, disabled }: Props) {
  const [pasted, setPasted] = useState('')
  const [pasteError, setPasteError] = useState<string | null>(null)
  const [extraKeys, setExtraKeys] = useState<SshKey[]>([])

  const selected = useMemo(
    () => modules.filter((m) => value.modules.includes(m.id)),
    [modules, value.modules],
  )
  const needsKeys = selected.some((m) => m.uses_ssh_keys)
  const allKeys = [...hostKeys, ...extraKeys]

  function toggleModule(id: string) {
    const next = value.modules.includes(id)
      ? value.modules.filter((m) => m !== id)
      : [...value.modules, id]
    onChange({ ...value, modules: next })
  }

  function setParam(name: string, next: string) {
    onChange({ ...value, params: { ...value.params, [name]: next } })
  }

  function toggleKey(line: string) {
    const next = value.ssh_keys.includes(line)
      ? value.ssh_keys.filter((k) => k !== line)
      : [...value.ssh_keys, line]
    onChange({ ...value, ssh_keys: next })
  }

  async function addPastedKey() {
    const text = pasted.trim()
    if (!text) return
    try {
      // The server is the authority on what counts as an acceptable key.
      const key = await api.validateSshKey(text)
      if (!allKeys.some((k) => k.line === key.line)) setExtraKeys((k) => [...k, key])
      onChange({ ...value, ssh_keys: [...new Set([...value.ssh_keys, key.line])] })
      setPasted('')
      setPasteError(null)
    } catch (cause) {
      setPasteError((cause as Error).message)
    }
  }

  if (modules.length === 0) {
    return (
      <p className="hint">
        No bootstrap modules found. Drop <span className="mono">.sh</span> files into{' '}
        <span className="mono">modules/</span> or{' '}
        <span className="mono">~/.config/lemondx/modules</span>.
      </p>
    )
  }

  return (
    <>
      <div className="module-list">
        {modules.map((module) => {
          const on = value.modules.includes(module.id)
          return (
            <div key={module.id} className={`module${on ? ' module-on' : ''}`}>
              <label className="checkbox">
                <input
                  type="checkbox"
                  checked={on}
                  disabled={disabled}
                  onChange={() => toggleModule(module.id)}
                />
                <span>
                  <strong>{module.name}</strong>
                  {module.uses_ssh_keys && <span className="vm-tag">SSH KEYS</span>}
                  <span className="module-id mono">{module.id}</span>
                </span>
              </label>
              {module.description && <p className="module-desc">{module.description}</p>}

              {on && module.params.length > 0 && (
                <div className="module-params">
                  {module.params.map((param) => (
                    <div className="field" key={param.name}>
                      <label htmlFor={`p-${param.name}`}>{param.name}</label>
                      <input
                        id={`p-${param.name}`}
                        className="input mono"
                        value={value.params[param.name] ?? param.default}
                        placeholder={param.default}
                        disabled={disabled}
                        onChange={(event) => setParam(param.name, event.target.value)}
                      />
                      {param.description && (
                        <span className="hint">{param.description}</span>
                      )}
                    </div>
                  ))}
                </div>
              )}
            </div>
          )
        })}
      </div>

      {needsKeys && (
        <div className="field" style={{ marginTop: 4 }}>
          <label>SSH public keys</label>
          {allKeys.length === 0 && (
            <span className="hint">
              No keys found in <span className="mono">~/.ssh</span>. Paste one below.
            </span>
          )}
          {allKeys.map((key) => (
            <label className="checkbox key-row" key={key.line}>
              <input
                type="checkbox"
                checked={value.ssh_keys.includes(key.line)}
                disabled={disabled}
                onChange={() => toggleKey(key.line)}
              />
              <span className="list-row-main">
                <strong>{key.source ?? key.comment ?? key.type}</strong>{' '}
                <span className="faint">{key.type}</span>
                <span className="mono faint key-fp">{key.fingerprint}</span>
              </span>
            </label>
          ))}

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
                  addPastedKey()
                }
              }}
            />
            <button type="button" className="btn" onClick={addPastedKey}
              disabled={disabled || !pasted.trim()}>
              Add
            </button>
          </div>
          {pasteError && (
            <span className="hint" style={{ color: 'var(--danger)' }}>{pasteError}</span>
          )}
          <span className="hint">
            Public keys only — never paste a private key.
          </span>
        </div>
      )}
    </>
  )
}
