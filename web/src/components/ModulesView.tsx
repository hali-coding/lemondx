import { useCallback, useEffect, useState } from 'react'
import { api } from '../lib/api'
import type { BootstrapModule, BootstrapProfile } from '../lib/types'
import { ConfirmDialog } from './ConfirmDialog'
import { ModuleUploadDialog } from './ModuleUploadDialog'
import { PlusIcon, TrashIcon } from './Icons'

interface Props {
  onNotify: (kind: 'success' | 'error' | 'info', title: string, detail?: string) => void
}

/** Manage bootstrap modules: defaults, saved settings, uploads, and profiles. */
export function ModulesView({ onNotify }: Props) {
  const [modules, setModules] = useState<BootstrapModule[] | null>(null)
  const [profiles, setProfiles] = useState<BootstrapProfile[]>([])
  const [error, setError] = useState<string | null>(null)
  const [showUpload, setShowUpload] = useState(false)
  const [pendingDelete, setPendingDelete] = useState<string | null>(null)
  const [pendingProfile, setPendingProfile] = useState<string | null>(null)
  const [drafts, setDrafts] = useState<Record<string, Record<string, string>>>({})
  const [busy, setBusy] = useState<string | null>(null)

  const load = useCallback((signal?: AbortSignal) => {
    Promise.all([api.modules(signal), api.bootstrapProfiles(signal)])
      .then(([loadedModules, loadedProfiles]) => {
        setModules(loadedModules)
        setProfiles(loadedProfiles)
        setDrafts({})
        setError(null)
      })
      .catch((cause) => {
        if ((cause as Error).name !== 'AbortError') setError((cause as Error).message)
      })
  }, [])

  useEffect(() => {
    const controller = new AbortController()
    load(controller.signal)
    return () => controller.abort()
  }, [load])

  async function toggleDefault(module: BootstrapModule) {
    setBusy(module.id)
    try {
      await api.updateModuleSettings(module.id, { is_default: !module.is_default })
      onNotify('success', module.is_default
        ? `${module.name} is no longer pre-selected`
        : `${module.name} will be pre-selected for new containers`)
      load()
    } catch (cause) {
      onNotify('error', 'Could not update the module', (cause as Error).message)
    } finally {
      setBusy(null)
    }
  }

  async function saveParams(module: BootstrapModule) {
    const draft = drafts[module.id]
    if (!draft) return
    setBusy(module.id)
    try {
      await api.updateModuleSettings(module.id, { params: draft })
      onNotify('success', `Saved defaults for ${module.name}`)
      load()
    } catch (cause) {
      onNotify('error', 'Could not save settings', (cause as Error).message)
    } finally {
      setBusy(null)
    }
  }

  async function removeModule() {
    const id = pendingDelete
    if (!id) return
    try {
      await api.deleteModule(id)
      onNotify('success', `Deleted ${id}`)
      load()
    } catch (cause) {
      onNotify('error', 'Could not delete', (cause as Error).message)
    } finally {
      setPendingDelete(null)
    }
  }

  async function removeProfile() {
    const name = pendingProfile
    if (!name) return
    try {
      await api.deleteBootstrapProfile(name)
      onNotify('success', `Deleted profile “${name}”`)
      load()
    } catch (cause) {
      onNotify('error', 'Could not delete the profile', (cause as Error).message)
    } finally {
      setPendingProfile(null)
    }
  }

  function setDraft(id: string, name: string, value: string) {
    setDrafts((current) => ({ ...current, [id]: { ...(current[id] ?? {}), [name]: value } }))
  }

  if (error) {
    return (
      <div className="banner banner-error">
        <div className="banner-body"><p style={{ margin: 0 }}>{error}</p></div>
      </div>
    )
  }
  if (!modules) {
    return (
      <div className="card">
        <div className="loading-wrap"><span className="spinner" /> Loading modules…</div>
      </div>
    )
  }

  const defaults = modules.filter((m) => m.is_default)

  return (
    <>
      <div className="section-head">
        <h2>Modules</h2>
        <button className="btn btn-primary btn-sm" onClick={() => setShowUpload(true)}>
          <PlusIcon /> Upload module
        </button>
      </div>

      <p className="hint" style={{ marginBottom: 14 }}>
        {defaults.length > 0 ? (
          <>
            Pre-selected for new containers:{' '}
            <strong>{defaults.map((m) => m.name).join(', ')}</strong>.
          </>
        ) : (
          <>No modules are pre-selected yet. Tick “Default” to start new containers with one.</>
        )}
      </p>

      {modules.map((module) => {
        const draft = drafts[module.id] ?? {}
        const dirty = Object.keys(draft).length > 0
        return (
          <section className="card module-card" key={module.id}>
            <header className="module-card-head">
              <div className="module-card-title">
                <h3>{module.name}</h3>
                <span className="mono faint">{module.id}</span>
                <span className={`badge ${module.builtin ? 'badge-dim' : 'badge-info'}`}>
                  {module.builtin ? 'built in' : 'uploaded'}
                </span>
                {module.uses_ssh_keys && <span className="badge badge-warn">ssh keys</span>}
              </div>
              <label className="checkbox">
                <input
                  type="checkbox"
                  checked={module.is_default}
                  disabled={busy === module.id}
                  onChange={() => toggleDefault(module)}
                />
                Default
              </label>
              {!module.builtin && (
                <button
                  className="btn btn-sm btn-icon btn-danger"
                  aria-label={`Delete ${module.id}`}
                  onClick={() => setPendingDelete(module.id)}
                >
                  <TrashIcon />
                </button>
              )}
            </header>

            <div className="module-card-body">
              {module.description && <p className="module-desc-full">{module.description}</p>}

              {module.params.length === 0 ? (
                <p className="hint">No settings.</p>
              ) : (
                <>
                  <div className="module-params-grid">
                    {module.params.map((param) => (
                      <div className="field" key={param.name}>
                        <label htmlFor={`s-${module.id}-${param.name}`}>
                          {param.name}
                          {param.saved && <span className="badge badge-ok">saved</span>}
                        </label>
                        <input
                          id={`s-${module.id}-${param.name}`}
                          className="input mono"
                          value={draft[param.name] ?? param.value}
                          placeholder={param.default}
                          disabled={busy === module.id}
                          onChange={(event) =>
                            setDraft(module.id, param.name, event.target.value)}
                        />
                        <span className="hint">
                          {param.description}
                          {param.saved && (
                            <> · module default is{' '}
                              <span className="mono">{param.default || '(empty)'}</span></>
                          )}
                        </span>
                      </div>
                    ))}
                  </div>
                  <div className="module-card-actions">
                    <button
                      className="btn btn-sm btn-primary"
                      disabled={!dirty || busy === module.id}
                      onClick={() => saveParams(module)}
                    >
                      {busy === module.id && <span className="spinner" />}
                      Save as defaults
                    </button>
                    <span className="hint">
                      Blank a field to fall back to the module's own default. Values
                      are also remembered automatically after a successful run.
                    </span>
                  </div>
                </>
              )}
            </div>
          </section>
        )
      })}

      <div className="section-head" style={{ marginTop: 26 }}>
        <h2>Bootstrap profiles</h2>
        <span className="faint" style={{ fontSize: 12.5 }}>
          saved module selections — save one from the create dialog
        </span>
      </div>

      {profiles.length === 0 ? (
        <div className="card">
          <div className="empty">
            <h3>No profiles yet</h3>
            <p>
              Pick your modules when creating a container, then use “Save as
              profile” to reuse that exact set later.
            </p>
          </div>
        </div>
      ) : (
        profiles.map((profile) => (
          <div className="list-row profile-row" key={profile.name}>
            <div className="list-row-main">
              <strong>{profile.name}</strong>
              {profile.description && (
                <div className="faint" style={{ fontSize: 12 }}>{profile.description}</div>
              )}
              <div className="mono faint" style={{ fontSize: 11.5, marginTop: 2 }}>
                {profile.modules.join(' → ')}
                {Object.keys(profile.params).length > 0 && (
                  <> · {Object.entries(profile.params)
                    .map(([k, v]) => `${k}=${v}`).join(' ')}</>
                )}
              </div>
            </div>
            <button
              className="btn btn-sm btn-icon btn-danger"
              aria-label={`Delete profile ${profile.name}`}
              onClick={() => setPendingProfile(profile.name)}
            >
              <TrashIcon />
            </button>
          </div>
        ))
      )}

      {showUpload && (
        <ModuleUploadDialog
          onCancel={() => setShowUpload(false)}
          onUploaded={(id) => {
            setShowUpload(false)
            onNotify('success', `Uploaded ${id}`)
            load()
          }}
        />
      )}

      {pendingDelete && (
        <ConfirmDialog
          title={`Delete ${pendingDelete}?`}
          message="The module file is removed from your config directory, along with any saved settings and its place in any profile. Containers already bootstrapped with it are unaffected."
          confirmLabel="Delete"
          danger
          onConfirm={removeModule}
          onCancel={() => setPendingDelete(null)}
        />
      )}

      {pendingProfile && (
        <ConfirmDialog
          title={`Delete profile “${pendingProfile}”?`}
          message="This removes the saved selection. The modules themselves are untouched."
          confirmLabel="Delete"
          danger
          onConfirm={removeProfile}
          onCancel={() => setPendingProfile(null)}
        />
      )}
    </>
  )
}
