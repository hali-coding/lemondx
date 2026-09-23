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

/** Mirror the server's bounds (store.APP_CHECK_INTERVAL / _TIMEOUT). */
const CHECK_INTERVAL = { min: 10, max: 86400, fallback: 60 }
const CHECK_TIMEOUT = { min: 1, max: 300, fallback: 10 }

const CHECK_PLACEHOLDER = `# Exit 0 ok, 1 warning, 2 critical, 3 unknown.
# The first line printed is shown as the status.
if curl -fsS -o /dev/null http://localhost/; then
  echo "OK - web server answering"; exit 0
fi
echo "CRITICAL - web server not answering"; exit 2`

function seconds(text: string, bounds: { min: number; max: number; fallback: number }) {
  if (!text.trim()) return bounds.fallback
  const value = Number(text)
  return Number.isInteger(value) && value >= bounds.min && value <= bounds.max ? value : null
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
  const [checkScript, setCheckScript] = useState(template?.app_check?.script ?? '')
  const [checkInterval, setCheckInterval] = useState(
    template?.app_check ? String(template.app_check.interval_seconds) : '')
  const [checkTimeout, setCheckTimeout] = useState(
    template?.app_check ? String(template.app_check.timeout_seconds) : '')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const { modules, profiles, hostKeys, reloadProfiles } = useBootstrapData()

  const nameValid = TEMPLATE_NAME_RULE.test(name.trim())
  const prefixValid = !prefix.trim() || PREFIX_RULE.test(prefix.trim())
  const problems = specProblems(spec, modules, true)
  const hasCheck = checkScript.trim() !== ''
  const interval = seconds(checkInterval, CHECK_INTERVAL)
  const timeout = seconds(checkTimeout, CHECK_TIMEOUT)
  const checkProblem = !hasCheck ? null
    : interval === null ? `The interval is a whole number of seconds from ${CHECK_INTERVAL.min} to ${CHECK_INTERVAL.max}.`
    : timeout === null ? `The timeout is a whole number of seconds from ${CHECK_TIMEOUT.min} to ${CHECK_TIMEOUT.max}.`
    : timeout >= interval ? 'The timeout must be shorter than the interval.'
    : !spec.start ? 'An app check needs the instance started.'
    : null
  const canSubmit = nameValid && prefixValid && !busy && !problems.blocked && !checkProblem
  const shownPrefix = prefix.trim() || defaultPrefix(name.trim() || 'instance')

  async function submit(event: React.FormEvent) {
    event.preventDefault()
    if (!canSubmit) return
    setBusy(true)
    setError(null)
    const resolved = resolvedSpec(spec)
    try {
      // The server refuses to delete a template a stack launches, which a
      // rename would only discover after saving the copy -- so ask first.
      if (template && template.name !== name.trim()) {
        const users = (await api.stacks()).filter((stack) => stack.stages.some(
          (stage) => stage.steps.some(
            (step) => step.type === 'launch' && step.template === template.name)))
        if (users.length) {
          setError(`Stack${users.length > 1 ? 's' : ''} ${users.map((s) => `“${s.name}”`).join(', ')} `
            + `launch${users.length > 1 ? '' : 'es'} “${template.name}”, so it cannot be renamed. `
            + 'Point them at another template first.')
          setBusy(false)
          return
        }
      }
      const saved = await api.saveTemplate(name.trim(), {
        ...resolved,
        bootstrap: savableSelection(modules, resolved.bootstrap),
        description: description.trim(),
        name_prefix: prefix.trim() || undefined,
        app_check: hasCheck
          ? { script: checkScript, interval_seconds: interval!, timeout_seconds: timeout! }
          : null,
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

        <details className="bootstrap-section" open={hasCheck}>
          <summary>
            App health check
            {hasCheck && <span className="badge badge-info">every {interval ?? '?'}s</span>}
          </summary>
          <p className="hint" style={{ marginBottom: 10 }}>
            Optional. A script run inside each instance from this template, alongside the
            usual health check, answering like a Nagios plugin: exit 0 ok, 1 warning,
            2 critical, 3 unknown. A <span className="mono">#!</span> line picks another
            interpreter; without one it runs under <span className="mono">/bin/sh</span>.
            Instances without a check always report their app as ok.
          </p>
          <div className="field">
            <label htmlFor="t-check">Script</label>
            <textarea id="t-check" className="input mono param-text" rows={7}
              value={checkScript} disabled={busy} spellCheck={false}
              onChange={(e) => setCheckScript(e.target.value)}
              placeholder={CHECK_PLACEHOLDER} />
          </div>
          <div className="grid-2">
            <div className="field">
              <label htmlFor="t-check-interval">Run every (seconds)</label>
              <input id="t-check-interval" className="input" inputMode="numeric"
                value={checkInterval} disabled={busy || !hasCheck}
                onChange={(e) => setCheckInterval(e.target.value)}
                placeholder={String(CHECK_INTERVAL.fallback)} aria-invalid={hasCheck && interval === null} />
            </div>
            <div className="field">
              <label htmlFor="t-check-timeout">Timeout (seconds)</label>
              <input id="t-check-timeout" className="input" inputMode="numeric"
                value={checkTimeout} disabled={busy || !hasCheck}
                onChange={(e) => setCheckTimeout(e.target.value)}
                placeholder={String(CHECK_TIMEOUT.fallback)} aria-invalid={hasCheck && timeout === null} />
            </div>
          </div>
          <span className="hint">
            {checkProblem
              ? <span style={{ color: 'var(--danger)' }}>{checkProblem}</span>
              : 'A check that overruns its timeout counts as critical. Criticals in a row, as many as the health settings’ failure streak, make the instance unhealthy; one alone, a warning or an unknown makes it degraded.'}
          </span>
        </details>

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
