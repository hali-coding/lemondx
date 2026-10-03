import { MAX_OCCURRENCES, occurrenceKey } from '../lib/bootstrap'
import type { BootstrapModule, BootstrapSelection } from '../lib/types'

interface Props {
  modules: BootstrapModule[]
  value: BootstrapSelection
  onChange: (next: BootstrapSelection) => void
  disabled?: boolean
  /** Show secrets as asked for on launch instead of as inputs (templates). */
  secretsAtLaunch?: boolean
}

/**
 * Module checkboxes and their parameters. Key selection is deliberately not
 * here: see SshKeyPicker, which the create dialog renders at the top level.
 *
 * A repeatable module can be added more than once -- two NFS mounts -- and each time has
 * its own parameters: the first under NAME, the nth under NAME@n, which is how
 * the server tells them apart (`bootstrap.param_key()`).
 */
export function BootstrapPicker({ modules, value, onChange, disabled, secretsAtLaunch }: Props) {
  const countOf = (id: string) => value.modules.filter((m) => m === id).length

  function toggleModule(id: string) {
    const next = value.modules.includes(id)
      ? value.modules.filter((m) => m !== id)
      : [...value.modules, id]
    onChange({ ...value, modules: next })
  }

  function addAnother(module: BootstrapModule) {
    const n = countOf(module.id) + 1
    // A fresh occurrence starts from the module's own values, never from a
    // removed one's leftovers under the same NAME@n.
    const params = { ...value.params }
    for (const param of module.params) delete params[occurrenceKey(param.name, n)]
    onChange({ ...value, modules: [...value.modules, module.id], params })
  }

  /** Drop the nth time `module` was added; the ones after it move up a place. */
  function removeOccurrence(module: BootstrapModule, n: number) {
    const count = countOf(module.id)
    let seen = 0
    const nextModules = value.modules.filter((m) => m !== module.id || ++seen !== n)
    const params = { ...value.params }
    for (const param of module.params) {
      for (let j = n; j < count; j += 1) {
        const later = params[occurrenceKey(param.name, j + 1)]
        if (later === undefined) delete params[occurrenceKey(param.name, j)]
        else params[occurrenceKey(param.name, j)] = later
      }
      delete params[occurrenceKey(param.name, count)]
    }
    onChange({ ...value, modules: nextModules, params })
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
          const count = countOf(module.id)
          const on = count > 0
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
                  {count > 1 && <span className="badge badge-info">×{count}</span>}
                  {module.uses_ssh_keys && <span className="vm-tag">SSH KEYS</span>}
                  <span className="module-id mono">{module.id}</span>
                </span>
              </label>
              {module.description && <p className="module-desc">{module.description}</p>}

              {on && Array.from({ length: count }, (_, index) => index + 1).map((n) => (
                <div key={n} className="module-params">
                  {count > 1 && (
                    <div className="module-occurrence">
                      <strong>{n === 1 ? 'First' : `#${n}`}</strong>
                      <button type="button" className="btn btn-ghost btn-sm" disabled={disabled}
                        onClick={() => removeOccurrence(module, n)}>
                        Remove
                      </button>
                    </div>
                  )}
                  {module.params.length === 0 && count > 1 && (
                    <span className="hint">No parameters: it runs again as it is.</span>
                  )}
                  {module.params.map((param) => (
                    <ParamField key={param.name} param={param} n={n} value={value}
                      disabled={disabled} secretsAtLaunch={secretsAtLaunch}
                      onChange={setParam} />
                  ))}
                </div>
              ))}
              {on && module.repeatable && (
                <button type="button" className="btn btn-ghost btn-sm module-again"
                  disabled={disabled || count >= MAX_OCCURRENCES}
                  title="Run this module again in the same instance, with its own parameters"
                  onClick={() => addAnother(module)}>
                  + Add another
                </button>
              )}
            </div>
          )
        })}
      </div>

    </>
  )
}

/** One parameter of one occurrence of a module. */
function ParamField({ param, n, value, disabled, secretsAtLaunch, onChange }: {
  param: BootstrapModule['params'][number]
  n: number
  value: BootstrapSelection
  disabled?: boolean
  secretsAtLaunch?: boolean
  onChange: (key: string, next: string) => void
}) {
  const key = occurrenceKey(param.name, n)
  const id = `p-${key}`
  if (param.secret && secretsAtLaunch) {
    return (
      <div className="field">
        <label>
          {param.name}
          <span className="badge badge-warn">secret</span>
        </label>
        <div className="input mono secret-placeholder">asked for on launch</div>
        {param.description && <span className="hint">{param.description}</span>}
      </div>
    )
  }
  // A repeat's secret left empty shares the first one's, as the runner does.
  const shared = param.secret && n > 1 && !value.params[key] && !!value.params[param.name]
  return (
    <div className="field">
      <label htmlFor={id}>
        {param.name}
        {param.secret && <span className="badge badge-warn">secret</span>}
      </label>
      {param.multiline && !param.secret ? (
        <textarea
          id={id}
          className="input mono param-text"
          rows={8}
          spellCheck={false}
          value={value.params[key] ?? param.value}
          placeholder={param.default}
          disabled={disabled}
          onChange={(event) => onChange(key, event.target.value)}
        />
      ) : (
        <input
          id={id}
          className="input mono"
          // new-password stops the browser offering the user's own
          // saved credentials for a container's database.
          type={param.secret ? 'password' : 'text'}
          autoComplete={param.secret ? 'new-password' : 'off'}
          value={value.params[key] ?? param.value}
          placeholder={shared ? 'same as the first' : param.default}
          disabled={disabled}
          aria-invalid={param.secret && !value.params[key] && !shared}
          onChange={(event) => onChange(key, event.target.value)}
        />
      )}
      {(param.description || param.secret) && (
        <span className="hint">
          {param.description}
          {param.saved && n === 1 && (
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
  )
}
