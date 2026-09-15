import { useEffect, useMemo, useState } from 'react'
import { api } from '../lib/api'
import type { BootstrapModule, ModuleSource } from '../lib/types'
import { ConfirmDialog } from './ConfirmDialog'
import { ChevronLeftIcon, PencilIcon, TrashIcon } from './Icons'

/** Where a module is referenced, so deleting or changing it is an informed choice. */
export interface ModuleUsage {
  profiles: string[]
  templates: string[]
}

export function ModuleOrigin({ module }: { module: BootstrapModule }) {
  if (module.shadows_builtin) return <span className="badge badge-info">customised</span>
  if (module.builtin) return <span className="badge badge-dim">built in</span>
  if (module.editable) return <span className="badge badge-info">uploaded</span>
  return <span className="badge badge-dim" title="From a directory in $LEMONDX_MODULES">external</span>
}

interface Props {
  module: BootstrapModule
  usage: ModuleUsage
  onBack: () => void
  /** After a save; `updated` when the server returned the new record directly. */
  onChanged: (updated?: BootstrapModule) => Promise<unknown>
  onDeleted: () => void
  onNotify: (kind: 'success' | 'error' | 'info', title: string, detail?: string) => void
}

// Header keys the runner reads; shown distinctly so the metadata stands out
// from the script it describes.
const HEADER_LINE = /^#\s*(name|description|order|uses|param|secret|os)\s*:/i

/** One module: its settings, its script, and where it is used. */
export function ModuleDetail({ module, usage, onBack, onChanged, onDeleted, onNotify }: Props) {
  const [tab, setTab] = useState<'settings' | 'script'>('settings')
  // Only the fields typed into, so a reload (after toggling pre-select, say)
  // refreshes the rest without losing them.
  const [drafts, setDrafts] = useState<Record<string, string>>({})
  const [saving, setSaving] = useState(false)
  const [source, setSource] = useState<ModuleSource | null>(null)
  const [sourceVersion, setSourceVersion] = useState(0)
  const [sourceError, setSourceError] = useState<string | null>(null)
  // Null while viewing; the text being edited otherwise.
  const [script, setScript] = useState<string | null>(null)
  const [scriptError, setScriptError] = useState<string | null>(null)
  const [savingScript, setSavingScript] = useState(false)
  const [confirm, setConfirm] = useState<'leave' | 'delete' | null>(null)
  const [deleting, setDeleting] = useState(false)
  const [togglingDefault, setTogglingDefault] = useState(false)

  const settable = module.params.filter((p) => !p.secret)
  const valueOf = (name: string, fallback: string) => drafts[name] ?? fallback
  const changed = settable.filter((p) => valueOf(p.name, p.value) !== p.value)
  const scriptDirty = script !== null && script !== source?.content
  const dirty = changed.length > 0 || scriptDirty

  useEffect(() => {
    const controller = new AbortController()
    api.moduleSource(module.id, controller.signal)
      .then((loaded) => { setSource(loaded); setSourceError(null) })
      .catch((cause) => {
        if ((cause as Error).name !== 'AbortError') setSourceError((cause as Error).message)
      })
    return () => controller.abort()
  }, [module.id, module.shadows_builtin, sourceVersion])

  // Closing the tab mid-edit should not silently throw work away.
  useEffect(() => {
    if (!dirty) return
    const warn = (event: BeforeUnloadEvent) => { event.preventDefault() }
    window.addEventListener('beforeunload', warn)
    return () => window.removeEventListener('beforeunload', warn)
  }, [dirty])

  const lines = useMemo(() => (source?.content ?? '').replace(/\n$/, '').split('\n'), [source])

  async function saveSettings() {
    setSaving(true)
    try {
      // Equal to the module's own default means "no override", so the value
      // follows the module if its default changes later.
      const params = Object.fromEntries(settable.map((p) => {
        const value = valueOf(p.name, p.value)
        return [p.name, value === p.default ? '' : value]
      }))
      const updated = await api.updateModuleSettings(module.id, { params })
      setDrafts({})
      await onChanged(updated)
      onNotify('success', `Saved settings for ${module.name}`)
    } catch (cause) {
      onNotify('error', 'Could not save settings', (cause as Error).message)
    } finally {
      setSaving(false)
    }
  }

  async function toggleDefault() {
    setTogglingDefault(true)
    try {
      const updated = await api.updateModuleSettings(module.id, { is_default: !module.is_default })
      await onChanged(updated)
    } catch (cause) {
      onNotify('error', 'Could not update the module', (cause as Error).message)
    } finally {
      setTogglingDefault(false)
    }
  }

  async function saveScript() {
    if (script === null) return
    setSavingScript(true)
    setScriptError(null)
    try {
      // Saving over a built-in writes a copy that shadows it; the server
      // refuses that without overwrite, and this button is the confirmation.
      await api.uploadModule(module.id, script, true)
      setSourceVersion((version) => version + 1)
      setScript(null)
      await onChanged()
      onNotify('success', module.editable
        ? `Saved ${module.id}`
        : `Saved your copy of ${module.id}`,
        module.editable ? undefined : 'It replaces the built-in until you revert it.')
    } catch (cause) {
      setScriptError((cause as Error).message)
    } finally {
      setSavingScript(false)
    }
  }

  async function remove() {
    setDeleting(true)
    try {
      const result = await api.deleteModule(module.id)
      if (result.restored_builtin) {
        onNotify('success', `Reverted ${module.id} to the built-in module`)
        setConfirm(null)
        setScript(null)
        await onChanged()
      } else {
        onNotify('success', `Deleted ${module.id}`)
        onDeleted()
      }
    } catch (cause) {
      onNotify('error', 'Could not delete', (cause as Error).message)
      setConfirm(null)
    } finally {
      setDeleting(false)
    }
  }

  const usedBy = [
    ...usage.profiles.map((name) => ({ kind: 'profile', name })),
    ...usage.templates.map((name) => ({ kind: 'template', name })),
  ]

  return (
    <>
      <button type="button" className="btn btn-ghost btn-sm module-back"
        onClick={() => dirty ? setConfirm('leave') : onBack()}>
        <ChevronLeftIcon /> All modules
      </button>

      <div className="card module-detail-head">
        <div className="module-detail-title">
          <div style={{ minWidth: 0 }}>
            <div className="module-detail-name">
              <h2>{module.name}</h2>
              <span className="mono faint">{module.id}</span>
              <ModuleOrigin module={module} />
              {module.uses_ssh_keys && <span className="vm-tag">SSH KEYS</span>}
            </div>
            {module.description && <p className="module-detail-desc">{module.description}</p>}
          </div>
          <div className="module-detail-actions">
            {module.editable && (
              <button className="btn btn-sm btn-danger" onClick={() => setConfirm('delete')}>
                <TrashIcon size={13} />
                {module.shadows_builtin ? 'Revert to built-in' : 'Delete'}
              </button>
            )}
          </div>
        </div>

        <dl className="module-facts">
          <div>
            <dt>Runs</dt>
            <dd>
              order <span className="mono">{module.order}</span>
              <span className="faint"> · lower runs first</span>
            </dd>
          </div>
          <div>
            <dt>Distros</dt>
            <dd>{module.os.length ? <span className="mono">{module.os.join(', ')}</span> : 'any'}</dd>
          </div>
          <div>
            <dt>Used by</dt>
            <dd>
              {usedBy.length === 0 ? <span className="faint">no profiles or templates</span>
                : usedBy.map((u, index) => (
                  <span key={`${u.kind}-${u.name}`}>
                    {index > 0 && ', '}
                    <span className="faint">{u.kind} </span>{u.name}
                  </span>
                ))}
            </dd>
          </div>
          <div>
            <dt>New instances</dt>
            <dd>
              <label className="checkbox">
                <input type="checkbox" checked={module.is_default} disabled={togglingDefault}
                  onChange={toggleDefault} />
                Pre-select in the create dialog
              </label>
            </dd>
          </div>
        </dl>
      </div>

      <div className="storage-toolbar">
        <div className="tabs storage-tabs" role="tablist" aria-label="Module sections">
          <button className="tab" role="tab" aria-selected={tab === 'settings'}
            onClick={() => setTab('settings')}>
            Settings <span className="tab-count">{module.params.length}</span>
            {changed.length > 0 && <span className="tab-dot" aria-label="unsaved" />}
          </button>
          <button className="tab" role="tab" aria-selected={tab === 'script'}
            onClick={() => setTab('script')}>
            Script
            {scriptDirty && <span className="tab-dot" aria-label="unsaved" />}
          </button>
        </div>
      </div>

      {tab === 'settings' ? (
        <div className="card">
          {module.params.length === 0 ? (
            <div className="empty">
              <h3>No settings</h3>
              <p>This module runs the same way every time. Its script declares no{' '}
                <span className="mono"># param:</span> lines.</p>
            </div>
          ) : (
            <>
              <div className="param-list">
                {module.params.map((param) => {
                  const value = valueOf(param.name, param.value)
                  const differs = !param.secret && value !== param.default
                  const edited = !param.secret && value !== param.value
                  return (
                    <div className="param-row" key={param.name}>
                      <div className="param-about">
                        <label htmlFor={`p-${param.name}`} className="param-name mono">
                          {param.name}
                        </label>
                        <div className="param-badges">
                          {param.secret && <span className="badge badge-warn">secret</span>}
                          {edited ? <span className="badge badge-info">edited</span>
                            : param.saved && <span className="badge badge-ok">saved</span>}
                        </div>
                        {param.description && <p className="param-desc">{param.description}</p>}
                      </div>
                      <div className="param-control">
                        {param.secret ? (
                          <>
                            <div className="input mono secret-placeholder">asked for on every run</div>
                            <span className="hint param-default">Never saved and never shown in logs.</span>
                          </>
                        ) : (
                          <>
                            <input id={`p-${param.name}`} className="input mono" value={value}
                              placeholder={param.default ? undefined : '(empty)'}
                              disabled={saving} autoComplete="off" spellCheck={false}
                              onChange={(event) =>
                                setDrafts({ ...drafts, [param.name]: event.target.value })}
                              onKeyDown={(event) => {
                                if (event.key === 'Enter' && changed.length > 0) saveSettings()
                              }} />
                            <span className="hint param-default">
                              {differs ? (
                                <>
                                  Module default{' '}
                                  <span className="mono">{param.default || '(empty)'}</span>
                                  <span aria-hidden>·</span>
                                  <button type="button" className="link-btn" disabled={saving}
                                    onClick={() => setDrafts({ ...drafts, [param.name]: param.default })}>
                                    Reset
                                  </button>
                                </>
                              ) : 'The module’s default'}
                            </span>
                          </>
                        )}
                      </div>
                    </div>
                  )
                })}
              </div>
              {settable.length > 0 && (
                <div className="param-footer">
                  <span className="hint">
                    {changed.length > 0
                      ? `${changed.length} unsaved change${changed.length === 1 ? '' : 's'}`
                      : 'These fill the Bootstrap section when this module is picked. Values from a successful run are saved here too.'}
                  </span>
                  <div className="btn-group">
                    <button className="btn btn-sm" disabled={saving || changed.length === 0}
                      onClick={() => setDrafts({})}>
                      Discard
                    </button>
                    <button className="btn btn-sm btn-primary"
                      disabled={saving || changed.length === 0} onClick={saveSettings}>
                      {saving && <span className="spinner" />}
                      Save settings
                    </button>
                  </div>
                </div>
              )}
            </>
          )}
        </div>
      ) : (
        <div className="card">
          <div className="script-bar">
            <span className="mono faint truncate" title={source?.path}>{source?.path ?? '…'}</span>
            {script === null ? (
              module.editable || module.builtin ? (
                <button className="btn btn-sm" disabled={!source}
                  onClick={() => { setScript(source?.content ?? ''); setScriptError(null) }}>
                  <PencilIcon size={13} /> {module.editable ? 'Edit' : 'Customise'}
                </button>
              ) : (
                <span className="hint">Read-only: edit it where it lives.</span>
              )
            ) : (
              <div className="btn-group">
                <button className="btn btn-sm" disabled={savingScript}
                  onClick={() => { setScript(null); setScriptError(null) }}>
                  Cancel
                </button>
                <button className="btn btn-sm btn-primary" disabled={savingScript || !scriptDirty}
                  onClick={saveScript}>
                  {savingScript && <span className="spinner" />}
                  {module.editable ? 'Save script' : 'Save my copy'}
                </button>
              </div>
            )}
          </div>

          {script !== null && !module.editable && (
            <div className="script-note">
              This is a built-in module. Saving writes your own copy to the modules
              directory, which is used in its place; <strong>Revert to built-in</strong>{' '}
              deletes the copy again. Updates to lemondx will not change your copy.
            </div>
          )}
          {scriptError && <div className="script-note script-error">{scriptError}</div>}
          {sourceError && <div className="script-note script-error">{sourceError}</div>}

          {script !== null ? (
            <textarea className="input mono module-source script-editor" value={script}
              spellCheck={false} disabled={savingScript} autoFocus
              onChange={(event) => setScript(event.target.value)}
              onKeyDown={(event) => {
                if ((event.ctrlKey || event.metaKey) && event.key === 's') {
                  event.preventDefault()
                  if (scriptDirty) saveScript()
                }
              }} />
          ) : source ? (
            <div className="code-view" role="region" aria-label={`${module.id} script`}>
              <pre>
                {lines.map((line, index) => (
                  <code key={index}
                    className={HEADER_LINE.test(line) ? 'code-meta'
                      : /^\s*#/.test(line) ? 'code-comment' : undefined}>
                    {line || ' '}
                  </code>
                ))}
              </pre>
            </div>
          ) : !sourceError && (
            <div className="loading-wrap"><span className="spinner" /> Loading script…</div>
          )}
          {script !== null && (
            <div className="param-footer">
              <span className="hint">
                Runs under <span className="mono">/bin/sh</span> with the prelude's helpers
                (<span className="mono">pkg_install</span>, <span className="mono">svc_enable</span>,{' '}
                <span className="mono">install_ssh_keys</span>). Checked with{' '}
                <span className="mono">sh -n</span> on save; Ctrl+S saves.
              </span>
            </div>
          )}
        </div>
      )}

      {confirm === 'leave' && (
        <ConfirmDialog
          title="Discard unsaved changes?"
          message={`Your edits to ${module.name} have not been saved.`}
          confirmLabel="Discard"
          danger
          onConfirm={onBack}
          onCancel={() => setConfirm(null)}
        />
      )}

      {confirm === 'delete' && (
        <ConfirmDialog
          title={module.shadows_builtin ? `Revert ${module.id}?` : `Delete ${module.id}?`}
          message={module.shadows_builtin
            ? 'Your copy of the script is deleted and the built-in module is used again. Saved settings, defaults, profiles and templates keep referring to it.'
            : `The module file is removed, along with its saved settings and its place in ${
              usedBy.length ? usedBy.map((u) => `${u.kind} “${u.name}”`).join(', ') : 'any profile'
            }. Instances already bootstrapped with it are unaffected.`}
          confirmLabel={module.shadows_builtin ? 'Revert' : 'Delete'}
          danger
          busy={deleting}
          onConfirm={remove}
          onCancel={() => setConfirm(null)}
        />
      )}
    </>
  )
}
