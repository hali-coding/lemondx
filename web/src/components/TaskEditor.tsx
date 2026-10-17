import { useEffect, useMemo, useState } from 'react'
import type { FormEvent } from 'react'
import { api, calls } from '../lib/api'
import { inputEnv, paramsReferring, secretEnv, TASK_ENV } from '../lib/tasks'
import type {
  BootstrapModule, BootstrapSelection, CronPreview, InstanceTemplate, NodeGroup, SavedTask, Task,
  TaskInput, TaskRequest,
} from '../lib/types'
import { BootstrapPicker } from './BootstrapPicker'
import { HelpButton, HelpPanel } from './HelpTip'
import { PlusIcon, TrashIcon } from './Icons'
import { Modal } from './Modal'
import { TargetPicker } from './TargetPicker'
import type { Targets } from './TargetPicker'

interface Props {
  /** The task being edited, or null for a new one. */
  task: Task | null
  templates: InstanceTemplate[]
  modules: BootstrapModule[]
  /** What it can be placed on: groups first, member names after. */
  groups: NodeGroup[]
  nodes: string[]
  onSaved: (saved: SavedTask) => void
  onCancel: () => void
}

const NAME = /^[A-Za-z0-9][A-Za-z0-9 _.-]{0,63}$/
const VALUE_NAME = /^[A-Za-z_][A-Za-z0-9_]{0,63}$/
const TIMEOUT = { low: 60, high: 86400, fallback: 900 }
const SCRIPT_LIMIT = 64 * 1024
const SCRIPT_PLACEHOLDER = `# Runs as root in a fresh instance of the template; exit 0 for success.
# Without a #! line: POSIX sh, with the module helpers (log, die, pkg_install…).
pkg_install curl
curl -fsS "https://example.com/hook?from=$WATERPUMP_TASK_NODE" \\
  -H "Authorization: Bearer $WATERPUMP_SECRET_API_KEY" \\
  --data @"\${WATERPUMP_TASK_PAYLOAD:-/dev/null}"`
const EMPTY_JOB: BootstrapSelection = { modules: [], params: {}, ssh_keys: [] }
// The browser's own zone first: what a person writing "02:00" usually means.
const LOCAL_ZONE = Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC'

type InputRow = TaskInput & { name: string }
type SecretRow = { name: string; description: string }

/** The task form: a template, the job's modules, a schedule, and what a run is handed. */
export function TaskEditor({ task, templates, modules, groups, nodes, onSaved, onCancel }: Props) {
  const [name, setName] = useState(task?.name ?? '')
  const [description, setDescription] = useState(task?.description ?? '')
  const [template, setTemplate] = useState(task?.template ?? templates[0]?.name ?? '')
  const [job, setJob] = useState<BootstrapSelection>(task?.bootstrap ?? EMPTY_JOB)
  const [jobKind, setJobKind] = useState<'modules' | 'script'>(task?.script ? 'script' : 'modules')
  const [script, setScript] = useState(task?.script ?? '')
  const [scheduled, setScheduled] = useState(!!task?.schedule)
  const [cron, setCron] = useState(task?.schedule?.cron ?? '0 2 * * *')
  const [timezone, setTimezone] = useState(task?.schedule?.timezone ?? LOCAL_ZONE)
  const [enabled, setEnabled] = useState(task?.enabled ?? true)
  const [inputs, setInputs] = useState<InputRow[]>(
    Object.entries(task?.inputs ?? {}).map(([n, spec]) => ({ ...spec, name: n })))
  const [secrets, setSecrets] = useState<SecretRow[]>(
    Object.entries(task?.secrets ?? {}).map(([n, spec]) => ({ name: n, ...spec })))
  const [targets, setTargets] = useState<Targets>({
    groups: task?.groups ?? [], nodes: task?.nodes ?? [] })
  const [timeout, setTimeoutText] = useState(String(task?.timeout_seconds ?? TIMEOUT.fallback))
  const [overlap, setOverlap] = useState<'skip' | 'allow'>(task?.overlap ?? 'skip')
  const [hooked, setHooked] = useState(!!task?.webhook)
  const [hookSecret, setHookSecret] = useState(task?.webhook?.secret ?? '')
  const [hookLabels, setHookLabels] = useState(
    (task?.webhook?.labels ?? ['self-hosted', 'Linux', 'X64']).join(', '))
  const [hookScope, setHookScope] = useState<'repo' | 'org'>(task?.webhook?.scope ?? 'repo')
  const [previewed, setPreview] = useState<CronPreview | null>(null)
  const [previewFailure, setPreviewError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [help, setHelp] = useState<'job' | 'inputs' | 'secrets' | null>(null)
  const toggleHelp = (topic: 'job' | 'inputs' | 'secrets') =>
    setHelp((open) => (open === topic ? null : topic))

  // Asked of the server, which is the one cron reader there is: what it says
  // here is what the scheduler will do.
  useEffect(() => {
    if (!scheduled || !cron.trim()) return
    const controller = new AbortController()
    const timer = window.setTimeout(() => {
      api.cronPreview(cron, timezone || 'UTC', controller.signal)
        .then((p) => { setPreview(p); setPreviewError(null) })
        .catch((cause) => {
          if ((cause as Error).name === 'AbortError') return
          setPreview(null)
          setPreviewError((cause as Error).message)
        })
    }, 300)
    return () => { controller.abort(); window.clearTimeout(timer) }
  }, [scheduled, cron, timezone])

  // Without a schedule there is nothing to preview, whatever was last fetched.
  const preview = scheduled && cron.trim() ? previewed : null
  const previewError = scheduled && cron.trim() ? previewFailure : null

  const timeoutValue = Number(timeout)
  const timeoutValid = Number.isInteger(timeoutValue)
    && timeoutValue >= TIMEOUT.low && timeoutValue <= TIMEOUT.high
  const nameValid = NAME.test(name.trim())
  const inputsValid = inputs.every((i) => VALUE_NAME.test(i.name))
    && new Set(inputs.map((i) => i.name.toUpperCase())).size === inputs.length
  const secretsValid = secrets.every((s) => VALUE_NAME.test(s.name))
  const labelList = useMemo(
    () => hookLabels.split(',').map((l) => l.trim()).filter(Boolean), [hookLabels])
  const hookValid = !hooked
    || (secrets.some((s) => s.name === hookSecret) && labelList.length > 0)
  const unattended = scheduled
    ? inputs.filter((i) => i.required && !i.default).map((i) => i.name) : []

  const request = useMemo((): TaskRequest => ({
    template,
    // One or the other: switching kind keeps what was typed, but sends only one.
    bootstrap: jobKind === 'modules' ? job : EMPTY_JOB,
    script: jobKind === 'script' ? script : '',
    description: description.trim(),
    schedule: scheduled ? { cron: cron.trim(), timezone: timezone.trim() || 'UTC' } : null,
    nodes: targets.nodes,
    groups: targets.groups,
    timeout_seconds: timeoutValid ? timeoutValue : TIMEOUT.fallback,
    overlap,
    inputs: Object.fromEntries(inputs.map(({ name: n, ...spec }) => [
      n, { ...spec, default: spec.secret ? '' : spec.default }])),
    secrets: Object.fromEntries(secrets.map((s) => [s.name, { description: s.description }])),
    webhook: hooked
      ? { provider: 'github', secret: hookSecret, labels: labelList, scope: hookScope } : null,
    enabled,
    ...(task && task.name !== name.trim() ? { previous: task.name } : {}),
  }), [template, job, jobKind, script, description, scheduled, cron, timezone, targets, timeoutValid,
    timeoutValue, overlap, inputs, secrets, hooked, hookSecret, labelList, hookScope,
    enabled, task, name])

  const placed = targets.groups.length > 0
  // What the chosen groups hold now, said under the box.
  const members = [...new Set([
    ...groups.filter((g) => targets.groups.includes(g.name)).flatMap((g) => g.members),
    ...targets.nodes])]
  const scriptBytes = new TextEncoder().encode(script).length
  const hasJob = jobKind === 'modules' ? job.modules.length > 0
    : script.trim().length > 0 && scriptBytes <= SCRIPT_LIMIT
  const canSubmit = !busy && nameValid && !!template && hasJob && timeoutValid
    && placed
    && inputsValid && secretsValid && unattended.length === 0 && hookValid
    && (!scheduled || !previewError)

  async function submit(event: FormEvent) {
    event.preventDefault()
    if (!canSubmit) return
    setBusy(true)
    setError(null)
    try {
      const saved = await api.saveTask(name.trim(), request)
      // A rename is a save under the new name; the old one goes after.
      if (task && task.name !== saved.name) await api.deleteTask(task.name)
      onSaved(saved)
    } catch (cause) {
      setError((cause as Error).message)
      setBusy(false)
    }
  }

  const updateInput = (index: number, change: Partial<InputRow>) =>
    setInputs((rows) => rows.map((row, i) => (i === index ? { ...row, ...change } : row)))
  const updateSecret = (index: number, change: Partial<SecretRow>) =>
    setSecrets((rows) => rows.map((row, i) => (i === index ? { ...row, ...change } : row)))

  return (
    <Modal
      title={task ? `Edit “${task.name}”` : 'New task'}
      subtitle="Job modules run in a throwaway instance of a template, on a schedule or when invoked."
      onClose={busy ? () => {} : onCancel}
      api={calls.saveTask(name.trim(), request)}
      footer={
        <>
          <button type="button" className="btn" onClick={onCancel} disabled={busy}>Cancel</button>
          <button type="submit" form="task-form" className="btn btn-primary" disabled={!canSubmit}>
            {busy && <span className="spinner" />}
            {busy ? 'Saving…' : 'Save task'}
          </button>
        </>
      }
    >
      <form id="task-form" onSubmit={submit} style={{ display: 'contents' }}>
        <div className="grid-2">
          <div className="field">
            <label htmlFor="task-name">Task name</label>
            <input id="task-name" className="input" value={name} disabled={busy}
              onChange={(e) => setName(e.target.value)} placeholder="Nightly backup"
              autoComplete="off" aria-invalid={name.length > 0 && !nameValid} />
          </div>
          <div className="field">
            <label htmlFor="task-template">Template</label>
            <select id="task-template" className="input" value={template} disabled={busy}
              onChange={(e) => setTemplate(e.target.value)}>
              {templates.length === 0 && <option value="">No templates yet</option>}
              {templates.map((t) => <option key={t.name} value={t.name}>{t.name}</option>)}
            </select>
          </div>
        </div>
        <span className="hint" style={{ marginTop: -8 }}>
          Each run launches one instance of the template — its image, size, network and its
          own modules — runs the job below in it, and destroys it.
        </span>

        <div className="field">
          <label htmlFor="task-targets">Runs on</label>
          <TargetPicker id="task-targets" groups={groups} nodes={nodes} value={targets}
            onChange={setTargets} disabled={busy} invalid={!placed}
            placeholder="a node group — type to search" />
          <span className="hint">
            {!placed ? (
              <>Pick a node group: each run goes to one of its members, chosen by the
                cluster. A node can be added beside a group, never instead of one.</>
            ) : members.length ? (
              <>Any of {members.join(', ')} — whichever the cluster picks for each run.</>
            ) : (
              <span style={{ color: 'var(--warn)' }}>
                These groups have no members yet, so it runs nowhere until they do.
              </span>
            )}
          </span>
        </div>

        <div className="field">
          <label htmlFor="task-desc">Description</label>
          <input id="task-desc" className="input" value={description} disabled={busy}
            maxLength={200} onChange={(e) => setDescription(e.target.value)}
            placeholder="What it does" autoComplete="off" />
        </div>

        <details className="bootstrap-section" open>
          <summary>
            Job
            <HelpButton open={help === 'job'} onToggle={() => toggleHelp('job')}
              label="What the job is given" />
            {jobKind === 'modules' && job.modules.length > 0
              && <span className="badge badge-info">{job.modules.length} module(s)</span>}
            {jobKind === 'script' && script.trim()
              && <span className="badge badge-info">script</span>}
          </summary>
          {help === 'job' && (
            <JobHelp kind={jobKind} inputs={inputs} secrets={secrets}
              params={job.params} onClose={() => setHelp(null)} />
          )}
          <div className="stack-segmented" role="radiogroup" aria-label="What the job is"
            style={{ marginBottom: 10 }}>
            <button type="button" role="radio" aria-checked={jobKind === 'modules'}
              disabled={busy} onClick={() => setJobKind('modules')}>
              Modules
            </button>
            <button type="button" role="radio" aria-checked={jobKind === 'script'}
              disabled={busy} onClick={() => setJobKind('script')}>
              Paste a script
            </button>
          </div>
          {jobKind === 'modules' ? (
            <>
              <p className="hint" style={{ marginBottom: 10 }}>
                Run after the template&rsquo;s own modules. Its exit code is the run&rsquo;s.
                A parameter can say <span className="mono">{'{{inputs.NAME}}'}</span> or{' '}
                <span className="mono">{'{{secrets.NAME}}'}</span>; every input also reaches
                the job as <span className="mono">$WATERPUMP_INPUT_NAME</span>.
              </p>
              <BootstrapPicker modules={modules} value={job} onChange={setJob} disabled={busy}
                secretReferences />
            </>
          ) : (
            <>
              <div className="field">
                <label htmlFor="task-script">Script</label>
                <textarea id="task-script" className="input mono param-text" rows={12}
                  value={script} disabled={busy} spellCheck={false}
                  aria-invalid={scriptBytes > SCRIPT_LIMIT}
                  onChange={(e) => setScript(e.target.value)} placeholder={SCRIPT_PLACEHOLDER} />
              </div>
              <p className="hint">
                Run as root after the template&rsquo;s own modules; its exit code is the
                run&rsquo;s. Without a <span className="mono">#!</span> line it is POSIX{' '}
                <span className="mono">sh</span> with the module helpers, checked on save; with
                one, that interpreter runs it and must be in the image. Inputs and secrets
                arrive as environment variables — the <span className="mono">?</span> above
                lists them.
                {scriptBytes > SCRIPT_LIMIT && (
                  <span style={{ color: 'var(--danger)' }}> It is over 64 KiB.</span>
                )}
              </p>
            </>
          )}
        </details>

        <details className="bootstrap-section" open>
          <summary>
            Schedule
            {scheduled && preview && <span className="badge badge-info">{preview.text}</span>}
          </summary>
          <div className="stack-segmented" role="radiogroup" aria-label="When it runs">
            <button type="button" role="radio" aria-checked={!scheduled} disabled={busy}
              onClick={() => setScheduled(false)}>
              When invoked
            </button>
            <button type="button" role="radio" aria-checked={scheduled} disabled={busy}
              onClick={() => setScheduled(true)}>
              On a schedule
            </button>
          </div>
          {scheduled && (
            <>
              <div className="grid-2">
                <div className="field">
                  <label htmlFor="task-cron">Cron expression</label>
                  <input id="task-cron" className="input mono" value={cron} disabled={busy}
                    onChange={(e) => setCron(e.target.value)} placeholder="0 2 * * *"
                    autoComplete="off" aria-invalid={!!previewError} />
                </div>
                <div className="field">
                  <label htmlFor="task-tz">Time zone</label>
                  <input id="task-tz" className="input mono" value={timezone} disabled={busy}
                    onChange={(e) => setTimezone(e.target.value)} placeholder="UTC"
                    autoComplete="off" />
                </div>
              </div>
              <span className="hint">
                {previewError ? (
                  <span style={{ color: 'var(--danger)' }}>{previewError}</span>
                ) : preview ? (
                  <>Next: {preview.next.map((at) => new Date(at * 1000).toLocaleString())
                    .join(' · ')}</>
                ) : 'minute hour day-of-month month day-of-week, or @hourly, @daily…'}
              </span>
              <label className="check">
                <input type="checkbox" checked={enabled} disabled={busy}
                  onChange={(e) => setEnabled(e.target.checked)} />
                Schedule on <span className="faint">— off pauses it; invoking still works</span>
              </label>
              <p className="hint">
                Every member that may run it agrees which one does, by vote, so each
                occurrence runs at most once. A member whose clock is more than 10 seconds
                out is refused. An occurrence nobody could run — most members down — is
                skipped and logged, not caught up.
              </p>
            </>
          )}
        </details>

        <details className="bootstrap-section" open={inputs.length > 0}>
          <summary>
            Inputs
            <HelpButton open={help === 'inputs'} onToggle={() => toggleHelp('inputs')}
              label="How inputs are passed" />
            {inputs.length > 0 && <span className="badge badge-info">{inputs.length}</span>}
          </summary>
          {help === 'inputs' && (
            <InputsHelp task={name.trim() || 'NAME'} kind={jobKind} inputs={inputs}
              params={job.params} onClose={() => setHelp(null)} />
          )}
          <p className="hint" style={{ marginBottom: 10 }}>
            Values a caller hands one run, like a message. A scheduled run uses the defaults.
            A secret input is masked in output and never kept.
          </p>
          {inputs.map((row, index) => (
            <div className="task-row" key={index}>
              <input className="input mono" value={row.name} placeholder="NAME" disabled={busy}
                aria-label="Input name" aria-invalid={!VALUE_NAME.test(row.name)}
                onChange={(e) => updateInput(index, { name: e.target.value })} />
              <input className="input" value={row.secret ? '' : row.default}
                placeholder={row.secret ? 'no default for a secret' : 'default'}
                disabled={busy || row.secret} aria-label="Default"
                onChange={(e) => updateInput(index, { default: e.target.value })} />
              <input className="input" value={row.description} placeholder="description"
                disabled={busy} aria-label="Description"
                onChange={(e) => updateInput(index, { description: e.target.value })} />
              <label className="check">
                <input type="checkbox" checked={row.required} disabled={busy}
                  onChange={(e) => updateInput(index, { required: e.target.checked })} />
                required
              </label>
              <label className="check">
                <input type="checkbox" checked={row.secret} disabled={busy}
                  onChange={(e) => updateInput(index, { secret: e.target.checked })} />
                secret
              </label>
              <button type="button" className="btn btn-sm btn-icon btn-ghost" disabled={busy}
                aria-label={`Remove input ${row.name}`}
                onClick={() => setInputs((rows) => rows.filter((_, i) => i !== index))}>
                <TrashIcon />
              </button>
            </div>
          ))}
          {unattended.length > 0 && (
            <span className="hint" style={{ color: 'var(--danger)' }}>
              A scheduled run has nobody to give it {unattended.join(', ')}: give a default,
              or make it optional.
            </span>
          )}
          <button type="button" className="btn btn-sm" disabled={busy}
            onClick={() => setInputs((rows) => [...rows, {
              name: '', default: '', description: '', required: false, secret: false }])}>
            <PlusIcon /> Add input
          </button>
        </details>

        <details className="bootstrap-section" open={secrets.length > 0}>
          <summary>
            Secrets
            <HelpButton open={help === 'secrets'} onToggle={() => toggleHelp('secrets')}
              label="How secrets are kept and passed" />
            {secrets.length > 0 && <span className="badge badge-info">{secrets.length}</span>}
          </summary>
          {help === 'secrets' && (
            <SecretsHelp task={name.trim() || 'NAME'} kind={jobKind} secrets={secrets}
              params={job.params} onClose={() => setHelp(null)} />
          )}
          <p className="hint" style={{ marginBottom: 10 }}>
            Declared here, set from the task&rsquo;s card once saved: sealed on every member,
            handed to the job {jobKind === 'script'
              ? <>as <span className="mono">$WATERPUMP_SECRET_NAME</span></>
              : <>by <span className="mono">{'{{secrets.NAME}}'}</span></>}, never shown
            again.
          </p>
          {secrets.map((row, index) => (
            <div className="task-row" key={index}>
              <input className="input mono" value={row.name} placeholder="NAME" disabled={busy}
                aria-label="Secret name" aria-invalid={!VALUE_NAME.test(row.name)}
                onChange={(e) => updateSecret(index, { name: e.target.value })} />
              <input className="input" value={row.description} placeholder="description"
                disabled={busy} aria-label="Description"
                onChange={(e) => updateSecret(index, { description: e.target.value })} />
              <button type="button" className="btn btn-sm btn-icon btn-ghost" disabled={busy}
                aria-label={`Remove secret ${row.name}`}
                onClick={() => setSecrets((rows) => rows.filter((_, i) => i !== index))}>
                <TrashIcon />
              </button>
            </div>
          ))}
          <button type="button" className="btn btn-sm" disabled={busy}
            onClick={() => setSecrets((rows) => [...rows, { name: '', description: '' }])}>
            <PlusIcon /> Add secret
          </button>
        </details>

        <details className="bootstrap-section" open={hooked}>
          <summary>
            GitHub webhook
            {hooked && <span className="badge badge-info">on</span>}
          </summary>
          <label className="check">
            <input type="checkbox" checked={hooked} disabled={busy}
              onChange={(e) => setHooked(e.target.checked)} />
            Start a run for each queued GitHub Actions job (a <span className="mono">workflow_job</span> webhook)
          </label>
          {hooked && (
            <>
              <div className="grid-2">
                <div className="field">
                  <label htmlFor="task-hook-secret">Signed with secret</label>
                  <select id="task-hook-secret" className="input" value={hookSecret}
                    disabled={busy} aria-invalid={!secrets.some((s) => s.name === hookSecret)}
                    onChange={(e) => setHookSecret(e.target.value)}>
                    <option value="">pick one of the task&rsquo;s secrets</option>
                    {secrets.filter((s) => VALUE_NAME.test(s.name)).map((s) => (
                      <option key={s.name} value={s.name}>{s.name}</option>
                    ))}
                  </select>
                </div>
                <div className="field">
                  <label htmlFor="task-hook-scope">GITHUB_URL is the</label>
                  <select id="task-hook-scope" className="input" value={hookScope} disabled={busy}
                    onChange={(e) => setHookScope(e.target.value as 'repo' | 'org')}>
                    <option value="repo">repository</option>
                    <option value="org">organisation</option>
                  </select>
                </div>
              </div>
              <div className="field">
                <label htmlFor="task-hook-labels">Labels the runner offers</label>
                <input id="task-hook-labels" className="input mono" value={hookLabels}
                  disabled={busy} aria-invalid={labelList.length === 0}
                  onChange={(e) => setHookLabels(e.target.value)} />
              </div>
              <span className="hint">
                In GitHub: Settings &rarr; Webhooks &rarr; Add webhook, payload URL{' '}
                <span className="mono">
                  {window.location.origin}/api/hooks/{encodeURIComponent(name.trim() || 'NAME')}
                </span>{' '}
                (through your public proxy), content type <span className="mono">application/json</span>,
                the same value as the secret, and only <em>Workflow jobs</em>. A job runs here
                only when every label its <span className="mono">runs-on</span> asks for is
                above; declare <span className="mono">GITHUB_URL</span>,{' '}
                <span className="mono">GITHUB_LABELS</span> and the other{' '}
                <span className="mono">GITHUB_*</span> inputs you want handed on.
                {overlap === 'skip' && ' Set overlap to "Run another alongside", or only one job runs at a time.'}
              </span>
            </>
          )}
        </details>

        <details className="bootstrap-section">
          <summary>Timeout and overlap</summary>
          <div className="grid-2">
            <div className="field">
              <label htmlFor="task-timeout">Timeout (seconds)</label>
              <input id="task-timeout" className="input" inputMode="numeric" value={timeout}
                disabled={busy} aria-invalid={!timeoutValid}
                onChange={(e) => setTimeoutText(e.target.value)} />
            </div>
            <div className="field">
              <label htmlFor="task-overlap">While a run is going</label>
              <select id="task-overlap" className="input" value={overlap} disabled={busy}
                onChange={(e) => setOverlap(e.target.value as 'skip' | 'allow')}>
                <option value="skip">Skip the next one</option>
                <option value="allow">Run another alongside</option>
              </select>
            </div>
          </div>
          <span className="hint">
            The timeout covers the whole run, from creating the instance to destroying it.
          </span>
        </details>

        {error && <div className="err">{error}</div>}
      </form>
    </Modal>
  )
}

type Kind = 'modules' | 'script'

function Code({ children }: { children: string }) {
  return <span className="mono">{children}</span>
}

/** Where each input ends up: its variable, and the parameters that name it. */
function InputsHelp({ task, kind, inputs, params, onClose }: {
  task: string
  kind: Kind
  inputs: InputRow[]
  params: Record<string, string>
  onClose: () => void
}) {
  const named = inputs.filter((i) => VALUE_NAME.test(i.name))
  const plain = named.find((i) => !i.secret)?.name ?? 'TARGET'
  const hidden = named.find((i) => i.secret)?.name
  return (
    <HelpPanel title="How inputs are passed" onClose={onClose}>
      <p>
        An input is a value one run is handed when it starts, like the message a function is
        invoked with. It is declared here and given per run:
      </p>
      <ul>
        <li><strong>Run</strong> on the task&rsquo;s card asks for each one.</li>
        <li>The CLI: <Code>{`waterpump task-run ${task} --input ${plain}=value`}</Code>
          {hidden && <> — a secret one such as <Code>{hidden}</Code> is read from{' '}
            <Code>{`$${hidden}`}</Code> or a prompt, never the command line</>}.</li>
        <li>The API, for a webhook: <Code>{`POST /api/tasks/${task}/invoke`}</Code> with{' '}
          <Code>{`{"inputs": {"${plain}": "value"}}`}</Code>.</li>
        <li>A <strong>scheduled</strong> run has nobody to ask, so it uses each default — which
          is why a required input needs one on a scheduled task.</li>
      </ul>
      <p>In the job, each input is:</p>
      <table className="help-table">
        <thead>
          <tr><th>Input</th><th>Variable</th>
            {kind === 'modules' && <th>Parameters naming it</th>}
            <th>On a schedule</th></tr>
        </thead>
        <tbody>
          {named.length === 0 && (
            <tr><td colSpan={4} className="faint">Add an input to see where it goes.</td></tr>
          )}
          {named.map((i) => {
            const users = paramsReferring(params, 'inputs', i.name)
            return (
              <tr key={i.name}>
                <td className="mono">
                  {i.name}{i.secret && <span className="badge badge-warn">secret</span>}
                </td>
                <td className="mono">${inputEnv(i.name)}</td>
                {kind === 'modules' && (
                  <td className="mono">{users.join(', ') || <span className="faint">—</span>}</td>
                )}
                <td>{i.secret ? <span className="faint">empty (no default)</span>
                  : i.default ? <span className="mono">{i.default}</span>
                    : <span className="faint">{i.required ? 'refused: required' : 'empty'}</span>}</td>
              </tr>
            )
          })}
        </tbody>
      </table>
      {kind === 'modules' && (
        <p>
          A module parameter takes one by saying <Code>{`{{inputs.${plain}}}`}</Code> in its
          value, anywhere in the text. A module&rsquo;s <em>secret</em> parameter may only be
          set to a secret input&rsquo;s reference, or a task secret&rsquo;s.
        </p>
      )}
      <p>
        A secret input is masked as <Code>********</Code> in all output, is never kept on
        the run&rsquo;s record, and cannot have a default — for a value kept with the task, use
        a secret below.
      </p>
    </HelpPanel>
  )
}

/** What a secret is, how it is set, and the one way each kind of job reads it. */
function SecretsHelp({ task, kind, secrets, params, onClose }: {
  task: string
  kind: Kind
  secrets: SecretRow[]
  params: Record<string, string>
  onClose: () => void
}) {
  const named = secrets.filter((s) => VALUE_NAME.test(s.name))
  const example = named[0]?.name ?? 'API_KEY'
  return (
    <HelpPanel title="How secrets are kept and passed" onClose={onClose}>
      <p>
        A secret is a value kept <em>with the task</em> — an API key, a database password —
        where an input is given per run. Here it is only declared; the value is never part of
        the task&rsquo;s definition.
      </p>
      <ul>
        <li><strong>Setting it:</strong> once the task is saved, the key button for it on the
          task&rsquo;s card, or <Code>{`waterpump task-secret-set ${task} ${example}`}</Code>
          {' '}(from <Code>{`$${example}`}</Code>, a prompt or stdin — never the command
          line).</li>
        <li><strong>Keeping it:</strong> sealed on every member under that node&rsquo;s own
          key, and copied to members as they join or come back. It is never shown again — it
          can only be replaced or cleared.</li>
        <li><strong>Using it:</strong> a run whose job needs a secret with no value is refused
          before anything is created.</li>
      </ul>
      <p>The job reads each one as:</p>
      <table className="help-table">
        <thead>
          <tr><th>Secret</th><th>{kind === 'script' ? 'Variable' : 'Parameters naming it'}</th></tr>
        </thead>
        <tbody>
          {named.length === 0 && (
            <tr><td colSpan={2} className="faint">Add a secret to see where it goes.</td></tr>
          )}
          {named.map((s) => {
            const users = paramsReferring(params, 'secrets', s.name)
            return (
              <tr key={s.name}>
                <td className="mono">{s.name}</td>
                <td className="mono">
                  {kind === 'script' ? `$${secretEnv(s.name)}`
                    : users.join(', ') || <span className="faint">none yet — set a parameter
                      to {`{{secrets.${s.name}}}`}</span>}
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
      <p>
        {kind === 'script' ? (
          <>A script gets every secret as an environment variable; nothing is written into its
            text.</>
        ) : (
          <>A module gets a secret only through a parameter set to{' '}
            <Code>{`{{secrets.${example}}}`}</Code> — the one value a module&rsquo;s own secret
            parameter may hold.</>
        )}{' '}Wherever it appears in the output, it is masked as <Code>********</Code>.
      </p>
    </HelpPanel>
  )
}

/** Everything the job is handed, for the kind of job this is. */
function JobHelp({ kind, inputs, secrets, params, onClose }: {
  kind: Kind
  inputs: InputRow[]
  secrets: SecretRow[]
  params: Record<string, string>
  onClose: () => void
}) {
  const named = inputs.filter((i) => VALUE_NAME.test(i.name))
  const kept = secrets.filter((s) => VALUE_NAME.test(s.name))
  const referenced = Object.entries(params).filter(([, v]) => /\{\{\s*(inputs|secrets)\./.test(v))
  return (
    <HelpPanel title="What the job is given" onClose={onClose}>
      <p>
        The job runs as root in a fresh instance of the template, after the template&rsquo;s
        own modules, and its exit code is the run&rsquo;s. It is given these environment
        variables:
      </p>
      <table className="help-table">
        <tbody>
          {TASK_ENV.map(([variable, meaning]) => (
            <tr key={variable}><td className="mono">${variable}</td><td>{meaning}</td></tr>
          ))}
          {named.map((i) => (
            <tr key={`i-${i.name}`}>
              <td className="mono">${inputEnv(i.name)}</td>
              <td>input {i.name}{i.secret ? ' (secret, masked)' : ''}</td>
            </tr>
          ))}
          {kind === 'script' && kept.map((s) => (
            <tr key={`s-${s.name}`}>
              <td className="mono">${secretEnv(s.name)}</td>
              <td>secret {s.name} (masked)</td>
            </tr>
          ))}
        </tbody>
      </table>
      {kind === 'modules' ? (
        <>
          <p>
            Each module also gets its own parameters. A parameter&rsquo;s value may name an
            input or a secret, filled in when the run starts:
          </p>
          <pre>{`POSTGRES_DB       = {{inputs.DB}}         a value given to this run
POSTGRES_PASSWORD = {{secrets.DB_PASSWORD}} a secret kept with the task`}</pre>
          {referenced.length > 0 && (
            <p>
              This job&rsquo;s: {referenced.map(([key, value]) => (
                <span key={key}><Code>{`${key} = ${value}`}</Code>{' '}</span>
              ))}
            </p>
          )}
          <p>
            Task secrets reach a module <em>only</em> through such a parameter; they are not in
            the environment of a module that does not ask.
          </p>
        </>
      ) : (
        <p>
          Nothing is filled into a script&rsquo;s text — read the variables, so a secret never
          lands in a file in the instance. Without a <Code>#!</Code> line the module helpers
          are there too: <Code>log</Code>, <Code>die</Code>, <Code>have</Code>,{' '}
          <Code>pkg_install</Code>, <Code>svc_enable</Code>.
        </p>
      )}
      <p>
        The payload, when a run is given one, is a file: <Code>{'cat "$WATERPUMP_TASK_PAYLOAD"'}</Code>.
        It is never put in the environment, the log or the run&rsquo;s record.
      </p>
    </HelpPanel>
  )
}
