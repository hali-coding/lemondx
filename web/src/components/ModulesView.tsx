import { useCallback, useEffect, useState } from 'react'
import { api } from '../lib/api'
import type { BootstrapModule, BootstrapProfile, InstanceTemplate } from '../lib/types'
import { ConfirmDialog } from './ConfirmDialog'
import { ModuleDetail, ModuleOrigin, type ModuleUsage } from './ModuleDetail'
import { ModuleUploadDialog } from './ModuleUploadDialog'
import { ChevronRightIcon, PlusIcon, TrashIcon } from './Icons'

interface Props {
  onNotify: (kind: 'success' | 'error' | 'info', title: string, detail?: string) => void
}

function usageOf(
  id: string, profiles: BootstrapProfile[], templates: InstanceTemplate[],
): ModuleUsage {
  return {
    profiles: profiles.filter((p) => p.modules.includes(id)).map((p) => p.name),
    templates: templates.filter((t) => t.bootstrap.modules.includes(id)).map((t) => t.name),
  }
}

/** Bootstrap modules and the profiles built from them: a list, and a page per module. */
export function ModulesView({ onNotify }: Props) {
  const [modules, setModules] = useState<BootstrapModule[] | null>(null)
  const [profiles, setProfiles] = useState<BootstrapProfile[]>([])
  const [templates, setTemplates] = useState<InstanceTemplate[]>([])
  const [error, setError] = useState<string | null>(null)
  const [tab, setTab] = useState<'modules' | 'profiles'>('modules')
  const [openId, setOpenId] = useState<string | null>(null)
  const [showUpload, setShowUpload] = useState(false)
  const [pendingProfile, setPendingProfile] = useState<string | null>(null)
  const [expandedProfile, setExpandedProfile] = useState<string | null>(null)
  const [busy, setBusy] = useState<string | null>(null)

  const load = useCallback((signal?: AbortSignal) => {
    return Promise.all([
      api.modules(signal),
      api.bootstrapProfiles(signal),
      // Only for "used by"; the tab still works if templates fail to load.
      api.templates(signal).catch(() => [] as InstanceTemplate[]),
    ])
      .then(([loadedModules, loadedProfiles, loadedTemplates]) => {
        setModules(loadedModules)
        setProfiles(loadedProfiles)
        setTemplates(loadedTemplates)
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
      const updated = await api.updateModuleSettings(module.id, { is_default: !module.is_default })
      setModules((current) => current?.map((m) => m.id === updated.id ? updated : m) ?? null)
      onNotify('success', updated.is_default
        ? `${module.name} will be pre-selected for new instances`
        : `${module.name} is no longer pre-selected`)
    } catch (cause) {
      onNotify('error', 'Could not update the module', (cause as Error).message)
    } finally {
      setBusy(null)
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

  function openModule(id: string) {
    setOpenId(id)
    window.scrollTo({ top: 0 })
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

  const opened = openId ? modules.find((m) => m.id === openId) : undefined
  if (opened) {
    return (
      <ModuleDetail
        key={opened.id}
        module={opened}
        usage={usageOf(opened.id, profiles, templates)}
        onBack={() => setOpenId(null)}
        onChanged={(updated) => {
          if (updated) {
            setModules((current) => current?.map((m) => m.id === updated.id ? updated : m) ?? null)
          }
          return load()
        }}
        onDeleted={() => { setOpenId(null); load() }}
        onNotify={onNotify}
      />
    )
  }

  const moduleNames = new Map(modules.map((m) => [m.id, m.name]))

  return (
    <>
      <div className="section-head">
        <h2>Modules</h2>
        <span className="faint" style={{ fontSize: 12.5 }}>
          shell scripts that set up an instance after it starts
        </span>
      </div>

      <div className="storage-toolbar">
        <div className="tabs storage-tabs" role="tablist" aria-label="Bootstrap objects">
          <button className="tab" role="tab" aria-selected={tab === 'modules'}
            onClick={() => setTab('modules')}>
            Modules <span className="tab-count">{modules.length}</span>
          </button>
          <button className="tab" role="tab" aria-selected={tab === 'profiles'}
            onClick={() => setTab('profiles')}>
            Profiles <span className="tab-count">{profiles.length}</span>
          </button>
        </div>
        {tab === 'modules' ? (
          <button className="btn btn-primary" onClick={() => setShowUpload(true)}>
            <PlusIcon /> Upload module
          </button>
        ) : (
          <span className="faint" style={{ fontSize: 12.5 }}>
            Save a profile from the create dialog’s Bootstrap section.
          </span>
        )}
      </div>

      {tab === 'modules' ? (
        <div className="card table-scroll">
          <table className="ctable module-table module-list-table">
            <thead>
              <tr>
                <th>Module</th>
                <th>Settings</th>
                <th>Used by</th>
                <th title="Ticked modules start selected when you create an instance">Pre-selected</th>
                <th aria-label="Open" />
              </tr>
            </thead>
            <tbody>
              {modules.map((module) => {
                const settable = module.params.filter((p) => !p.secret)
                const saved = settable.filter((p) => p.saved).length
                const secrets = module.params.length - settable.length
                const usage = usageOf(module.id, profiles, templates)
                const usedBy = usage.profiles.length + usage.templates.length
                return (
                  <tr key={module.id} tabIndex={0}
                    onClick={() => openModule(module.id)}
                    onKeyDown={(event) => {
                      if (event.target !== event.currentTarget) return
                      if (event.key === 'Enter' || event.key === ' ') {
                        event.preventDefault()
                        openModule(module.id)
                      }
                    }}>
                    <td>
                      <div className="cname">
                        {module.name}
                        <span className="mono faint module-id">{module.id}</span>
                        <ModuleOrigin module={module} />
                        {module.uses_ssh_keys && <span className="vm-tag">SSH KEYS</span>}
                      </div>
                      {module.description && (
                        <div className="cdesc module-row-desc">{module.description}</div>
                      )}
                    </td>
                    <td className="nowrap">
                      {module.params.length === 0 ? <span className="faint">none</span> : (
                        <>
                          {module.params.length} setting{module.params.length === 1 ? '' : 's'}
                          {saved > 0 && <span className="badge badge-ok module-count">{saved} saved</span>}
                          {secrets > 0 && <span className="badge badge-warn module-count">{secrets} secret</span>}
                        </>
                      )}
                    </td>
                    <td className="nowrap">
                      {usedBy === 0 ? <span className="faint">—</span> : (
                        <span title={[...usage.profiles.map((p) => `profile ${p}`),
                          ...usage.templates.map((t) => `template ${t}`)].join('\n')}>
                          {[usage.profiles.length && `${usage.profiles.length} profile${usage.profiles.length === 1 ? '' : 's'}`,
                            usage.templates.length && `${usage.templates.length} template${usage.templates.length === 1 ? '' : 's'}`]
                            .filter(Boolean).join(', ')}
                        </span>
                      )}
                    </td>
                    <td onClick={(event) => event.stopPropagation()}>
                      <label className="checkbox module-default">
                        <input type="checkbox" checked={module.is_default}
                          disabled={busy === module.id}
                          aria-label={`Pre-select ${module.name} for new instances`}
                          onChange={() => toggleDefault(module)} />
                      </label>
                    </td>
                    <td className="module-chevron"><ChevronRightIcon /></td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      ) : profiles.length === 0 ? (
        <div className="card">
          <div className="empty">
            <h3>No profiles yet</h3>
            <p>
              Pick modules, settings and keys when creating an instance, then use
              “Save as profile” to reuse that exact set later.
            </p>
          </div>
        </div>
      ) : (
        <div className="card table-scroll">
          <table className="ctable module-table">
            <thead>
              <tr>
                <th>Profile</th>
                <th>Runs</th>
                <th>Settings</th>
                <th>SSH keys</th>
                <th aria-label="Actions" />
              </tr>
            </thead>
            <tbody>
              {profiles.map((profile) => {
                const expanded = expandedProfile === profile.name
                const params = Object.entries(profile.params)
                const missing = profile.modules.filter((id) => !moduleNames.has(id)).length
                return [
                  <tr key={profile.name} tabIndex={0} aria-expanded={expanded}
                    onClick={() => setExpandedProfile(expanded ? null : profile.name)}
                    onKeyDown={(event) => {
                      if (event.target !== event.currentTarget) return
                      if (event.key === 'Enter' || event.key === ' ') {
                        event.preventDefault()
                        setExpandedProfile(expanded ? null : profile.name)
                      }
                    }}>
                    <td>
                      <div className="cname">
                        {profile.name}
                        {missing > 0 && (
                          <span className="badge badge-danger"
                            title="Modules this profile runs that no longer exist">
                            {missing} missing
                          </span>
                        )}
                      </div>
                      {profile.description && <div className="cdesc">{profile.description}</div>}
                    </td>
                    <td>
                      <div className="module-chain" onClick={(event) => event.stopPropagation()}>
                        {profile.modules.map((id, index) => (
                          <span key={id} className="module-chain-step">
                            {index > 0 && <span className="faint">→</span>}
                            {moduleNames.has(id) ? (
                              <button type="button" className="module-link"
                                onClick={() => openModule(id)}>{moduleNames.get(id)}</button>
                            ) : (
                              <span className="mono" style={{ color: 'var(--danger)' }}
                                title="This module no longer exists">{id}</span>
                            )}
                          </span>
                        ))}
                      </div>
                    </td>
                    <td className="nowrap">{params.length || <span className="faint">none</span>}</td>
                    <td className="nowrap">{profile.ssh_keys.length || <span className="faint">none</span>}</td>
                    <td onClick={(event) => event.stopPropagation()}>
                      <div className="row-actions">
                        <button className="btn btn-sm btn-icon btn-danger"
                          aria-label={`Delete profile ${profile.name}`}
                          onClick={() => setPendingProfile(profile.name)}>
                          <TrashIcon />
                        </button>
                      </div>
                    </td>
                  </tr>,
                  expanded && (
                    <tr key={`${profile.name}-detail`} className="module-profile-detail">
                      <td colSpan={5}>
                        {params.length === 0 && profile.ssh_keys.length === 0 ? (
                          <span className="faint">No settings or keys stored.</span>
                        ) : (
                          <dl className="kv module-profile-kv">
                            {params.map(([key, value]) => (
                              <div key={key} style={{ display: 'contents' }}>
                                <dt className="mono">{key}</dt>
                                <dd className="mono">{value || <span className="faint">(empty)</span>}</dd>
                              </div>
                            ))}
                            {profile.ssh_keys.map((key, index) => (
                              <div key={key} style={{ display: 'contents' }}>
                                <dt>{index === 0 ? 'SSH keys' : ''}</dt>
                                <dd className="mono truncate" title={key}>
                                  {key.split(' ').slice(0, 1).join('')} … {key.split(' ').slice(2).join(' ')}
                                </dd>
                              </div>
                            ))}
                          </dl>
                        )}
                      </td>
                    </tr>
                  ),
                ]
              })}
            </tbody>
          </table>
        </div>
      )}

      {showUpload && (
        <ModuleUploadDialog
          onCancel={() => setShowUpload(false)}
          onUploaded={(id) => {
            setShowUpload(false)
            onNotify('success', `Uploaded ${id}`)
            load().then(() => openModule(id))
          }}
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
