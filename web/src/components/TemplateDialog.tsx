import { useState } from 'react'
import { api } from '../lib/api'
import { savableSelection } from '../lib/bootstrap'
import type { InstanceSpec, InstanceTemplate, Synced } from '../lib/types'
import { useBootstrapData } from '../hooks/useBootstrapData'
import { blankSpec, resolvedSpec, specOf, specProblems } from '../lib/instance'
import { InstanceForm } from './InstanceForm'
import { Modal } from './Modal'

interface Props {
  /** The template being edited; absent when creating one. */
  template?: InstanceTemplate
  onCancel: () => void
  onSaved: (saved: Synced<InstanceTemplate>) => void
}

const TEMPLATE_NAME_RULE = /^[A-Za-z0-9][A-Za-z0-9 _.-]{0,63}$/
const PREFIX_RULE = /^[a-zA-Z][a-zA-Z0-9-]{0,49}$/

/** Mirrors the server's default, so the placeholder shows the real result. */
function defaultPrefix(name: string) {
  let slug = name.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '')
  if (!/^[a-z]/.test(slug)) slug = `instance-${slug}`.replace(/-+$/, '')
  return slug.slice(0, 50).replace(/-+$/, '')
}

export function TemplateDialog({ template, onCancel, onSaved }: Props) {
  const [name, setName] = useState(template?.name ?? '')
  const [description, setDescription] = useState(template?.description ?? '')
  const [prefix, setPrefix] = useState(template?.name_prefix ?? '')
  const [spec, setSpec] = useState<InstanceSpec>(() => template ? specOf(template) : blankSpec())
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const { modules, profiles, hostKeys, reloadProfiles } = useBootstrapData()

  const nameValid = TEMPLATE_NAME_RULE.test(name.trim())
  const prefixValid = !prefix.trim() || PREFIX_RULE.test(prefix.trim())
  const problems = specProblems(spec, modules, true)
  const canSubmit = nameValid && prefixValid && !busy && !problems.blocked
  const shownPrefix = prefix.trim() || defaultPrefix(name.trim() || 'instance')

  async function submit(event: React.FormEvent) {
    event.preventDefault()
    if (!canSubmit) return
    setBusy(true)
    setError(null)
    const resolved = resolvedSpec(spec)
    try {
      const saved = await api.saveTemplate(name.trim(), {
        ...resolved,
        bootstrap: savableSelection(modules, resolved.bootstrap),
        description: description.trim(),
        name_prefix: prefix.trim() || undefined,
      })
      // Saving is keyed by name, so a rename is a new record and the old one
      // has to go -- after the save, so a failure cannot lose both.
      if (template && template.name !== saved.name) {
        await api.deleteTemplate(template.name)
      }
      onSaved(saved)
    } catch (cause) {
      setError((cause as Error).message)
      setBusy(false)
    }
  }

  return (
    <Modal
      title={template ? `Edit “${template.name}”` : 'New template'}
      subtitle="Everything about an instance except its name, ready to launch in one click."
      onClose={busy ? () => {} : onCancel}
      footer={
        <>
          <button type="button" className="btn" onClick={onCancel} disabled={busy}>
            Cancel
          </button>
          <button type="submit" form="template-form" className="btn btn-primary"
            disabled={!canSubmit}>
            {busy && <span className="spinner" />}
            {busy ? 'Saving…' : 'Save template'}
          </button>
        </>
      }
    >
      <form id="template-form" onSubmit={submit} style={{ display: 'contents' }}>
        <div className="grid-2">
          <div className="field">
            <label htmlFor="t-name">Template name</label>
            <input id="t-name" className="input" value={name} disabled={busy}
              onChange={(e) => setName(e.target.value)} placeholder="Web server"
              autoComplete="off" aria-invalid={name.length > 0 && !nameValid} />
          </div>
          <div className="field">
            <label htmlFor="t-prefix">Instance names</label>
            <input id="t-prefix" className="input mono" value={prefix} disabled={busy}
              onChange={(e) => setPrefix(e.target.value)} placeholder={shownPrefix}
              autoComplete="off" aria-invalid={!prefixValid} />
          </div>
        </div>
        <span className="hint" style={{ marginTop: -8 }}>
          {name.length > 0 && !nameValid ? (
            <span style={{ color: 'var(--danger)' }}>
              Names use letters, digits, spaces, dots, dashes and underscores.
            </span>
          ) : !prefixValid ? (
            <span style={{ color: 'var(--danger)' }}>
              The prefix starts with a letter; letters, digits and dashes only.
            </span>
          ) : (
            <>
              Instances are named <span className="mono">{shownPrefix}-1</span>,{' '}
              <span className="mono">{shownPrefix}-2</span>, … using the lowest free numbers.
            </>
          )}
        </span>

        <div className="field">
          <label htmlFor="t-desc">Description</label>
          <input id="t-desc" className="input" value={description} disabled={busy}
            onChange={(e) => setDescription(e.target.value)} maxLength={200}
            placeholder="What these instances are for" autoComplete="off" />
        </div>

        <InstanceForm
          value={spec}
          onChange={setSpec}
          modules={modules}
          profiles={profiles}
          hostKeys={hostKeys}
          reloadProfiles={reloadProfiles}
          onError={setError}
          disabled={busy}
          secretsAtLaunch
        />

        {spec.profiles.length > 0 && (
          <span className="hint">
            Also applies {spec.profiles.length === 1 ? 'daemon profile' : 'daemon profiles'}{' '}
            <span className="mono">{spec.profiles.join(', ')}</span> (set with{' '}
            <span className="mono">lemondx template-save --profile</span>).
          </span>
        )}

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
