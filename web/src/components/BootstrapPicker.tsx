import type { BootstrapModule, BootstrapSelection } from '../lib/types'

interface Props {
  modules: BootstrapModule[]
  value: BootstrapSelection
  onChange: (next: BootstrapSelection) => void
  disabled?: boolean
}

/**
 * Module checkboxes and their parameters. Key selection is deliberately not
 * here: see SshKeyPicker, which the create dialog renders at the top level.
 */
export function BootstrapPicker({ modules, value, onChange, disabled }: Props) {
  function toggleModule(id: string) {
    const next = value.modules.includes(id)
      ? value.modules.filter((m) => m !== id)
      : [...value.modules, id]
    onChange({ ...value, modules: next })
  }

  function setParam(name: string, next: string) {
    onChange({ ...value, params: { ...value.params, [name]: next } })
  }



  if (modules.length === 0) {
    return (
      <p className="hint">
        No bootstrap modules found. Drop <span className="mono">.sh</span> files into{' '}
        <span className="mono">modules/</span> or{' '}
        <span className="mono">~/.local/share/lemondx/modules</span>.
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
                      <label htmlFor={`p-${param.name}`}>
                        {param.name}
                        {param.secret && <span className="badge badge-warn">secret</span>}
                      </label>
                      <input
                        id={`p-${param.name}`}
                        className="input mono"
                        // new-password stops the browser offering the user's own
                        // saved credentials for a container's database.
                        type={param.secret ? 'password' : 'text'}
                        autoComplete={param.secret ? 'new-password' : 'off'}
                        value={value.params[param.name] ?? param.value}
                        placeholder={param.default}
                        disabled={disabled}
                        aria-invalid={param.secret && !value.params[param.name]}
                        onChange={(event) => setParam(param.name, event.target.value)}
                      />
                      {(param.description || param.secret) && (
                        <span className="hint">
                          {param.description}
                          {param.saved && (
                            <span className="faint"> · saved default</span>
                          )}
                          {param.secret && (
                            <span className="faint">
                              {' '}· required, never saved or shown in logs
                            </span>
                          )}
                        </span>
                      )}
                    </div>
                  ))}
                </div>
              )}
            </div>
          )
        })}
      </div>

    </>
  )
}
