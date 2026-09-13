import { useEffect, useRef, useState } from 'react'
import { api } from '../lib/api'
import { savableSelection } from '../lib/bootstrap'
import type { CreateRequest, InstanceSpec, InstanceTemplate } from '../lib/types'
import { useBootstrapData } from '../hooks/useBootstrapData'
import { blankSpec, resolvedSpec, specOf, specProblems } from '../lib/instance'
import { InstanceForm } from './InstanceForm'
import { Modal } from './Modal'

interface Props {
  onCancel: () => void
  onCreate: (request: CreateRequest) => Promise<void>
  onNotify: (kind: 'success' | 'error' | 'info', title: string, detail?: string) => void
}

const NAME_RULE = /^[a-zA-Z][a-zA-Z0-9-]{0,61}$/
// A template prefix leaves room for "-<n>" within the instance name limit.
const PREFIX_RULE = /^[a-zA-Z][a-zA-Z0-9-]{0,49}$/

export function CreateDialog({ onCancel, onCreate, onNotify }: Props) {
  const [name, setName] = useState('')
  const [spec, setSpec] = useState<InstanceSpec>(blankSpec)
  const [touchedBootstrap, setTouchedBootstrap] = useState(false)
  const [templates, setTemplates] = useState<InstanceTemplate[]>([])
  const [templateName, setTemplateName] = useState('')
  const [busy, setBusy] = useState(false)
  const [savingTemplate, setSavingTemplate] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const { modules, profiles, hostKeys, reloadProfiles } = useBootstrapData()
  // The request only waits for the server to accept the create, but the dialog
  // can still be closed during it, so a refusal can arrive after it is gone.
  const mounted = useRef(true)

  useEffect(() => {
    mounted.current = true
    const controller = new AbortController()
    api.templates(controller.signal).then(setTemplates).catch(() => {})
    return () => {
      mounted.current = false
      controller.abort()
    }
  }, [])

  // Until the user touches the selection, it *is* whatever they marked as
  // default. Deriving this during render beats syncing it in an effect: there
  // is no intermediate state where the catalogue has loaded but the defaults
  // have not been applied.
  const value: InstanceSpec = touchedBootstrap ? spec : {
    ...spec,
    bootstrap: { ...spec.bootstrap, modules: modules.filter((m) => m.is_default).map((m) => m.id) },
  }

  function change(next: InstanceSpec) {
    // The form spreads the spec, so an untouched selection keeps its identity.
    if (next.bootstrap !== value.bootstrap) setTouchedBootstrap(true)
    setSpec(next)
  }

  function applyTemplate(chosen: string) {
    setTemplateName(chosen)
    const template = templates.find((t) => t.name === chosen)
    if (!template) return
    setTouchedBootstrap(true)
    setSpec(specOf(template))
  }

  async function saveAsTemplate() {
    const chosen = window.prompt('Save this instance setup as a template called:',
      templateName || '')
    if (!chosen?.trim()) return
    setSavingTemplate(true)
    setError(null)
    try {
      const resolved = resolvedSpec(value)
      const saved = await api.saveTemplate(chosen.trim(), {
        ...resolved,
        bootstrap: savableSelection(modules, resolved.bootstrap),
        // The name typed here is the natural stem for instances made from it.
        name_prefix: PREFIX_RULE.test(name.trim()) ? name.trim() : undefined,
      })
      setTemplateName(saved.name)
      setTemplates((current) =>
        [...current.filter((t) => t.name !== saved.name), saved]
          .sort((a, b) => a.name.localeCompare(b.name)))
      onNotify('success', `Saved template “${saved.name}”`,
        'Launch it from the Templates tab.')
    } catch (cause) {
      setError((cause as Error).message)
    } finally {
      setSavingTemplate(false)
    }
  }

  const nameValid = NAME_RULE.test(name)
  const problems = specProblems(value, modules, false)
  const canSubmit = nameValid && !busy && !problems.blocked
  const selected = value.bootstrap.modules.length

  async function submit(event: React.FormEvent) {
    event.preventDefault()
    if (!canSubmit) return
    setBusy(true)
    setError(null)
    const resolved = resolvedSpec(value)
    try {
      await onCreate({
        name: name.trim(),
        image: resolved.image,
        type: resolved.type,
        cpu: resolved.cpu,
        memory: resolved.memory,
        disk: resolved.disk || undefined,
        pool: resolved.pool || undefined,
        profiles: resolved.profiles.length > 0 ? resolved.profiles : undefined,
        ephemeral: resolved.ephemeral,
        start: resolved.start,
        bootstrap: selected > 0 ? resolved.bootstrap : undefined,
      })
      // Accepted: the create runs on the server from here, and the list shows it.
      if (mounted.current) onCancel()
    } catch (cause) {
      if (mounted.current) {
        setError((cause as Error).message)
        setBusy(false)
      } else {
        onNotify('error', `Could not create ${name.trim()}`, (cause as Error).message)
      }
    }
  }

  return (
    <Modal
      title="New container"
      subtitle="The image is downloaded on first use, which can take a minute."
      onClose={onCancel}
      footer={
        <>
          <button type="button" className="btn btn-ghost" style={{ marginRight: 'auto' }}
            onClick={saveAsTemplate}
            disabled={busy || savingTemplate || !value.image.trim() || problems.keysMissing}
            title="Keep everything except the name and any secrets, to launch again later">
            {savingTemplate && <span className="spinner" />}
            Save as template
          </button>
          <button type="button" className="btn" onClick={onCancel}>
            Cancel
          </button>
          <button type="submit" form="create-form" className="btn btn-primary" disabled={!canSubmit}>
            {busy && <span className="spinner" />}
            {busy ? 'Starting…' : 'Create'}
          </button>
        </>
      }
    >
      <form id="create-form" onSubmit={submit} style={{ display: 'contents' }}>
        {templates.length > 0 && (
          <div className="field">
            <label htmlFor="c-template">Template</label>
            <select id="c-template" className="select" value={templateName} disabled={busy}
              onChange={(event) => applyTemplate(event.target.value)}>
              <option value="">Start from a template…</option>
              {templates.map((template) => (
                <option key={template.name} value={template.name}>{template.name}</option>
              ))}
            </select>
          </div>
        )}

        <div className="field">
          <label htmlFor="c-name">Name</label>
          <input
            id="c-name"
            className="input"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="my-container"
            autoComplete="off"
            aria-invalid={name.length > 0 && !nameValid}
            disabled={busy}
          />
          {name.length > 0 && !nameValid && (
            <span className="hint" style={{ color: 'var(--danger)' }}>
              Start with a letter; letters, digits and dashes only.
            </span>
          )}
        </div>

        <InstanceForm
          value={value}
          onChange={change}
          modules={modules}
          profiles={profiles}
          hostKeys={hostKeys}
          reloadProfiles={reloadProfiles}
          onError={setError}
          disabled={busy}
        />



        {error && (
          <div className="banner banner-error" style={{ margin: 0, padding: '10px 12px' }}>
            <div className="banner-body">
              <p style={{ margin: 0, color: 'var(--danger)' }}>{error}</p>
            </div>
          </div>
        )}
      </form>
    </Modal>
  )
}
