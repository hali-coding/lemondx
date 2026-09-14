import { useCallback, useEffect, useState } from 'react'
import { api } from '../lib/api'
import type {
  BootstrapModule, Container, InstanceTemplate, LaunchedInstance, TemplateRun,
} from '../lib/types'
import { ConfirmDialog } from './ConfirmDialog'
import { CloseIcon, PencilIcon, PlusIcon, RestartIcon, TerminalIcon, TrashIcon } from './Icons'
import { Modal } from './Modal'
import { StatusBadge } from './StatusBadge'
import { TemplateDialog } from './TemplateDialog'

interface Props {
  containers: Container[] | null
  /** Whether the daemon can create instances yet; saving works regardless. */
  ready: boolean
  onNotify: (kind: 'success' | 'error' | 'info', title: string, detail?: string) => void
  /** What the server is running or last ran per template, from App's poll. */
  runs: TemplateRun[]
  onRunStarted: (run: TemplateRun) => void
  onOpen: (name: string) => void
  /** Refresh now, and let go of any of these instances the drawer shows. */
  onChanged: (removed: string[]) => void
}

const MAX_LAUNCH = 20

type Action = 'launch' | 'recreate' | 'destroy' | 'exec'


/**
 * The names a launch would use: the lowest free `<prefix>-<n>`. Only a
 * preview -- the server picks the real names when the launch starts.
 */
function previewNames(prefix: string, count: number, containers: Container[]) {
  const taken = new Set(containers.map((c) => c.name))
  const names: string[] = []
  for (let index = 1; names.length < count; index += 1) {
    const candidate = `${prefix}-${index}`
    if (!taken.has(candidate)) names.push(candidate)
  }
  return names
}

function secretParams(template: InstanceTemplate, modules: BootstrapModule[]) {
  return modules
    .filter((m) => template.bootstrap.modules.includes(m.id))
    .flatMap((m) => m.params)
    .filter((p) => p.secret)
}

/** Saved instance setups, each launchable as one or many identical instances. */
export function TemplatesView({
  containers, ready, runs: runList, onRunStarted, onNotify, onOpen, onChanged,
}: Props) {
  const [templates, setTemplates] = useState<InstanceTemplate[] | null>(null)
  const [modules, setModules] = useState<BootstrapModule[]>([])
  const [error, setError] = useState<string | null>(null)
  const [editing, setEditing] = useState<InstanceTemplate | 'new' | null>(null)
  const [pendingDelete, setPendingDelete] = useState<string | null>(null)
  const [counts, setCounts] = useState<Record<string, string>>({})
  // Only while the request that starts a run is out, which is brief: the
  // server answers once it has accepted the run, not when the run is done.
  const [starting, setStarting] = useState<Record<string, { action: Action; count: number }>>({})
  const [confirming, setConfirming] = useState<{ template: InstanceTemplate; action: Action } | null>(null)

  const load = useCallback((signal?: AbortSignal) => {
    Promise.all([api.templates(signal), api.modules(signal)])
      .then(([loadedTemplates, loadedModules]) => {
        setTemplates(loadedTemplates)
        setModules(loadedModules)
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

  const runs = Object.fromEntries(runList.map((run) => [run.template, run]))

  function busyWith(name: string) {
    const run = runs[name]
    if (run && run.finished_at === null) return { action: run.action, count: run.count }
    return starting[name]
  }

  async function dismiss(name: string) {
    try {
      await api.dismissTemplateRun(name)
    } catch { /* already gone, or someone started another; the poll will tell */ }
    onChanged([])
  }

  function countFor(name: string) {
    const parsed = Number.parseInt(counts[name] ?? '1', 10)
    return Number.isFinite(parsed) ? Math.min(Math.max(parsed, 1), MAX_LAUNCH) : 1
  }

  const membersOf = (template: InstanceTemplate) =>
    (containers ?? []).filter((c) => c.template === template.name)

  function requestLaunch(template: InstanceTemplate) {
    // Secrets are the one thing a template cannot keep, so only a template
    // that needs one costs a second click.
    if (secretParams(template, modules).length > 0) setConfirming({ template, action: 'launch' })
    else run(template, 'launch', [], {})
  }

  async function run(template: InstanceTemplate, action: Action, instances: string[],
                     params: Record<string, string>, command = '', timeout?: number) {
    const count = action === 'launch' ? countFor(template.name) : instances.length
    setStarting((current) => ({ ...current, [template.name]: { action, count } }))
    try {
      // Returns as soon as the server has accepted the run; the run itself
      // goes on there, and App reports how it ended from its poll.
      const started = action === 'launch'
        ? await api.launchTemplate(template.name, { count, params })
        : action === 'recreate'
          ? await api.recreateTemplateInstances(template.name, instances, params)
          : action === 'exec'
            ? await api.execTemplateInstances(template.name, command, instances, timeout)
            : await api.destroyTemplateInstances(template.name, instances)
      onRunStarted(started)
      // The drawer would be showing an instance about to be deleted.
      onChanged(action === 'exec' ? [] : instances)
    } catch (cause) {
      onNotify('error', `Could not ${action} from “${template.name}”`, (cause as Error).message)
    } finally {
      setStarting((current) => {
        const next = { ...current }
        delete next[template.name]
        return next
      })
    }
  }

  async function remove() {
    const name = pendingDelete
    if (!name) return
    try {
      await api.deleteTemplate(name)
      onNotify('success', `Deleted template “${name}”`)
      load()
    } catch (cause) {
      onNotify('error', 'Could not delete the template', (cause as Error).message)
    } finally {
      setPendingDelete(null)
    }
  }

  if (error) {
    return (
      <div className="banner banner-error">
        <div className="banner-body"><p style={{ margin: 0 }}>{error}</p></div>
      </div>
    )
  }

  return (
    <>
      <div className="section-head">
        <h2>Templates</h2>
        <span className="faint" style={{ fontSize: 12.5 }}>
          saved instance setups, launched one or many at a time
        </span>
        <div className="topbar-spacer" />
        <button className="btn btn-primary btn-sm" onClick={() => setEditing('new')}>
          <PlusIcon /> New template
        </button>
      </div>

      {templates === null ? (
        <div className="card">
          <div className="loading-wrap"><span className="spinner" /> Loading templates…</div>
        </div>
      ) : templates.length === 0 ? (
        <div className="card">
          <div className="empty">
            <h3>No templates yet</h3>
            <p>
              A template keeps an image, limits, bootstrap modules with all their
              settings, and SSH keys, so identical instances are one click away.
              Create one here, or fill in the new-container dialog and use “Save
              as template”.
            </p>
          </div>
        </div>
      ) : (
        <div className="template-grid">
          {templates.map((template) => {
            const secrets = secretParams(template, modules)
            const gone = template.bootstrap.modules.filter(
              (id) => modules.length > 0 && !modules.some((m) => m.id === id))
            const moduleNames = template.bootstrap.modules.map(
              (id) => modules.find((m) => m.id === id)?.name ?? id)
            const members = membersOf(template)
            const running = members.filter((c) => c.status === 'Running').length
            const count = countFor(template.name)
            const busy = busyWith(template.name)
            const preview = previewNames(template.name_prefix, count, containers ?? [])
            const run = runs[template.name]
            const finished = run && run.finished_at !== null && !busy ? run : null
            const result = finished?.result
            const limits = [
              template.cpu && `${template.cpu} CPU`,
              template.memory,
              template.disk && `${template.disk} disk`,
              template.pool && `pool ${template.pool}`,
              template.network && `network ${template.network}`,
            ].filter(Boolean).join(' · ')

            return (
              <section className="card template-card" key={template.name}>
                <header className="module-card-head">
                  <div className="module-card-title">
                    <h3>{template.name}</h3>
                    {template.type === 'virtual-machine' && <span className="vm-tag">VM</span>}
                    {template.ephemeral && <span className="vm-tag">EPH</span>}
                    {!template.secureboot && <span className="vm-tag">NO SECURE BOOT</span>}
                  </div>
                  <button className="btn btn-sm btn-icon btn-ghost"
                    aria-label={`Edit template ${template.name}`} title="Edit"
                    disabled={!!busy} onClick={() => setEditing(template)}>
                    <PencilIcon />
                  </button>
                  <button className="btn btn-sm btn-icon btn-danger"
                    aria-label={`Delete template ${template.name}`} title="Delete template"
                    disabled={!!busy} onClick={() => setPendingDelete(template.name)}>
                    <TrashIcon />
                  </button>
                </header>

                <div className="module-card-body template-body">
                  {template.description && (
                    <p className="module-desc-full">{template.description}</p>
                  )}
                  <dl className="kv template-facts">
                    <dt>Image</dt>
                    <dd className="mono">{template.image}</dd>
                    <dt>Limits</dt>
                    <dd>{limits || <span className="faint">none</span>}</dd>
                    <dt>Modules</dt>
                    <dd>
                      {moduleNames.length ? moduleNames.join(' → ') : <span className="faint">none</span>}
                    </dd>
                    {template.bootstrap.ssh_keys.length > 0 && (
                      <>
                        <dt>SSH keys</dt>
                        <dd>{template.bootstrap.ssh_keys.length} saved</dd>
                      </>
                    )}
                    {template.profiles.length > 0 && (
                      <>
                        <dt>Profiles</dt>
                        <dd className="mono">{template.profiles.join(', ')}</dd>
                      </>
                    )}
                    <dt>Instances</dt>
                    <dd>
                      {members.length === 0 ? (
                        <span className="faint">none yet</span>
                      ) : (
                        <span className="template-members">
                          {members.map((c) => (
                            <button key={c.name} type="button" className="template-member"
                              onClick={() => onOpen(c.name)} title={c.status}>
                              <span className={`dot${c.status === 'Running' ? ' dot-ok' : ''}`} />
                              {c.name}
                            </button>
                          ))}
                          <span className="faint">{running} running</span>
                        </span>
                      )}
                    </dd>
                  </dl>

                  {gone.length > 0 && (
                    <p className="hint" style={{ color: 'var(--danger)' }}>
                      Uses {gone.join(', ')}, which no longer exist. Edit the template
                      before launching or recreating from it.
                    </p>
                  )}

                  <div className="template-launch">
                    <input
                      className="input num"
                      type="number"
                      min={1}
                      max={MAX_LAUNCH}
                      value={counts[template.name] ?? '1'}
                      aria-label={`How many to launch from ${template.name}`}
                      disabled={!!busy}
                      onChange={(event) => setCounts((current) => ({
                        ...current, [template.name]: event.target.value }))}
                    />
                    <button className="btn btn-primary"
                      disabled={!ready || !!busy || gone.length > 0}
                      onClick={() => requestLaunch(template)}>
                      {busy?.action === 'launch' && <span className="spinner" />}
                      {busy?.action === 'launch' ? `Launching ${busy.count}…` : 'Launch'}
                    </button>
                    <span className="hint mono truncate" title={preview.join(', ')}>
                      {busy ? '' : `→ ${preview.length > 3
                        ? `${preview.slice(0, 2).join(', ')} … ${preview[preview.length - 1]}`
                        : preview.join(', ')}`}
                    </span>
                    {(members.length > 0 || (busy && busy.action !== 'launch')) && (
                      <div className="template-fleet">
                        <button className="btn btn-sm"
                          title="Run a shell command on the running instances from this template"
                          disabled={!!busy || running === 0}
                          onClick={() => setConfirming({ template, action: 'exec' })}>
                          {busy?.action === 'exec' ? <span className="spinner" /> : <TerminalIcon size={13} />}
                          {busy?.action === 'exec' ? `Running on ${busy.count}…` : 'Run command'}
                        </button>
                        <button className="btn btn-sm"
                          title="Delete every instance from this template and create it again, with the template as it is now"
                          disabled={!ready || !!busy || gone.length > 0}
                          onClick={() => setConfirming({ template, action: 'recreate' })}>
                          {busy?.action === 'recreate' ? <span className="spinner" /> : <RestartIcon size={13} />}
                          {busy?.action === 'recreate' ? `Recreating ${busy.count}…` : 'Recreate all'}
                        </button>
                        <button className="btn btn-sm btn-danger"
                          title="Stop and delete every instance from this template"
                          disabled={!ready || !!busy}
                          onClick={() => setConfirming({ template, action: 'destroy' })}>
                          {busy?.action === 'destroy' ? <span className="spinner" /> : <TrashIcon size={13} />}
                          {busy?.action === 'destroy' ? `Destroying ${busy.count}…` : 'Destroy all'}
                        </button>
                      </div>
                    )}
                  </div>
                  {(busy?.action === 'launch' || busy?.action === 'recreate') && (
                    <span className="hint">
                      Creating and bootstrapping — this can take a few minutes.
                    </span>
                  )}
                  {busy?.action === 'exec' && run?.command && (
                    <span className="hint">
                      Running <span className="mono">{run.command}</span> — output appears here
                      when every instance has finished.
                    </span>
                  )}
                  {secrets.length > 0 && !busy && (
                    <span className="hint">
                      Asks for {secrets.map((p) => p.name).join(' and ')} on launch and recreate.
                    </span>
                  )}

                  {finished && (
                    <div className="template-run-head">
                      <span className="truncate">
                        {finished.action === 'exec' ? (
                          <>Last command <span className="mono">{finished.command}</span></>
                        ) : <>Last {finished.action}</>}
                        {finished.finished_at && (
                          <> · finished {new Date(finished.finished_at * 1000).toLocaleTimeString()}</>
                        )}
                      </span>
                      <button type="button" className="btn btn-ghost btn-icon btn-sm"
                        aria-label={`Dismiss the last ${finished.action} of ${template.name}`}
                        title="Dismiss" onClick={() => dismiss(template.name)}>
                        <CloseIcon size={13} />
                      </button>
                    </div>
                  )}
                  {finished?.error && (
                    <p className="hint" style={{ color: 'var(--danger)' }}>{finished.error}</p>
                  )}
                  {result && finished?.action === 'exec' && (
                    <ExecResults instances={result.instances} onOpen={onOpen} />
                  )}
                  {result && finished?.action !== 'exec' && (
                    <ul className="template-results">
                      {result.instances.map((instance) => {
                        const failedModule = instance.container?.bootstrap?.modules
                          .find((m) => m.exit_code !== 0)
                        return (
                          <li key={instance.name}>
                            <span className={`badge ${instance.ok ? 'badge-ok' : 'badge-danger'}`}>
                              {!instance.ok ? 'failed' : instance.container ? 'ok' : 'deleted'}
                            </span>
                            {instance.container ? (
                              <button type="button" className="template-member"
                                onClick={() => onOpen(instance.name)}>
                                {instance.name}
                              </button>
                            ) : (
                              <span className="mono">{instance.name}</span>
                            )}
                            {instance.error && <span className="faint">{instance.error}</span>}
                            {failedModule && (
                              <span className="faint">module “{failedModule.name}” failed — open it for the log</span>
                            )}
                          </li>
                        )
                      })}
                    </ul>
                  )}
                </div>
              </section>
            )
          })}
        </div>
      )}

      {editing && (
        <TemplateDialog
          template={editing === 'new' ? undefined : editing}
          onCancel={() => setEditing(null)}
          onSaved={(saved) => {
            onNotify('success', `Saved template “${saved.name}”`)
            setEditing(null)
            load()
          }}
        />
      )}

      {confirming?.action === 'exec' && (
        <ExecDialog
          template={confirming.template}
          members={membersOf(confirming.template)}
          lastCommand={runs[confirming.template.name]?.command ?? ''}
          onCancel={() => setConfirming(null)}
          onConfirm={(instances, command, timeout) => {
            const { template } = confirming
            setConfirming(null)
            run(template, 'exec', instances, {}, command, timeout)
          }}
        />
      )}

      {confirming && confirming.action !== 'exec' && (
        <ActionDialog
          template={confirming.template}
          action={confirming.action}
          // Live, so what the dialog lists is what gets sent -- and if polling
          // shows a change, the list the user is agreeing to changes with it.
          members={membersOf(confirming.template)}
          secrets={confirming.action === 'destroy' ? []
            : secretParams(confirming.template, modules)}
          count={countFor(confirming.template.name)}
          onCancel={() => setConfirming(null)}
          onConfirm={(instances, params) => {
            const { template, action } = confirming
            setConfirming(null)
            run(template, action, instances, params)
          }}
        />
      )}

      {pendingDelete && (
        <ConfirmDialog
          title={`Delete template “${pendingDelete}”?`}
          message="This removes the saved setup. Instances already launched from it are untouched; destroy them first if they should go too."
          confirmLabel="Delete"
          danger
          onConfirm={remove}
          onCancel={() => setPendingDelete(null)}
        />
      )}
    </>
  )
}

interface ActionProps {
  template: InstanceTemplate
  action: Action
  members: Container[]
  secrets: BootstrapModule['params']
  count: number
  onCancel: () => void
  onConfirm: (instances: string[], params: Record<string, string>) => void
}

/**
 * Confirms a recreate or destroy by listing exactly which instances it hits,
 * and collects any secrets a launch or recreate needs -- those go to this run
 * and nowhere else.
 */
function ActionDialog({ template, action, members, secrets, count, onCancel, onConfirm }: ActionProps) {
  const [values, setValues] = useState<Record<string, string>>({})
  const complete = secrets.every((p) => values[p.name])
  const names = members.map((c) => c.name)
  const launch = action === 'launch'
  const canConfirm = complete && (launch || names.length > 0)

  const title = launch ? `Launch ${count} from “${template.name}”`
    : action === 'recreate' ? `Recreate ${names.length} from “${template.name}”?`
    : `Destroy ${names.length} from “${template.name}”?`
  const subtitle = launch
    ? 'Secrets are never saved, so they are entered for each launch. Every instance in this launch gets the same values.'
    : action === 'recreate'
      ? 'Each instance is stopped, deleted and created again with the same name, from the template as it is now. Everything inside them, including snapshots, is lost.'
      : 'Each instance is stopped and deleted, with its filesystem and snapshots. This cannot be undone. The template itself is kept.'

  return (
    <Modal
      title={title}
      subtitle={subtitle}
      onClose={onCancel}
      footer={
        <>
          <button type="button" className="btn" onClick={onCancel}>Cancel</button>
          <button type="submit" form="template-action-form"
            className={`btn ${launch ? 'btn-primary' : 'btn-danger'}`}
            disabled={!canConfirm}>
            {launch ? 'Launch' : action === 'recreate' ? `Recreate ${names.length}` : `Destroy ${names.length}`}
          </button>
        </>
      }
    >
      <form id="template-action-form" style={{ display: 'contents' }}
        onSubmit={(event) => {
          event.preventDefault()
          if (canConfirm) onConfirm(names, values)
        }}>
        {!launch && (
          names.length === 0 ? (
            <p className="hint">No instances from this template remain.</p>
          ) : (
            <ul className="template-results">
              {members.map((c) => (
                <li key={c.name}>
                  <StatusBadge status={c.status} />
                  <span className="mono">{c.name}</span>
                  {c.ipv4[0] && <span className="faint mono">{c.ipv4[0]}</span>}
                </li>
              ))}
            </ul>
          )
        )}

        {secrets.map((param) => (
          <div className="field" key={param.name}>
            <label htmlFor={`secret-${param.name}`}>
              {param.name}
              <span className="badge badge-warn">secret</span>
            </label>
            <input
              id={`secret-${param.name}`}
              className="input mono"
              type="password"
              // new-password stops the browser offering the user's own saved
              // credentials for a container's database.
              autoComplete="new-password"
              value={values[param.name] ?? ''}
              onChange={(event) => setValues((current) => ({
                ...current, [param.name]: event.target.value }))}
            />
            {param.description && <span className="hint">{param.description}</span>}
          </div>
        ))}
      </form>
    </Modal>
  )
}

/** Each instance's exit code and output from a command run across a template. */
function ExecResults({ instances, onOpen }: {
  instances: LaunchedInstance[]
  onOpen: (name: string) => void
}) {
  return (
    <div className="template-exec">
      {instances.map((instance) => {
        const outcome = instance.exec
        const stdout = outcome?.stdout.replace(/\n$/, '') ?? ''
        const stderr = outcome?.stderr.replace(/\n$/, '') ?? ''
        return (
          // Open the ones worth reading first: failures, or all of a small run.
          <details key={instance.name} open={!instance.ok || instances.length <= 3}>
            <summary>
              <span className={`badge ${instance.ok ? 'badge-ok' : 'badge-danger'}`}>
                {outcome ? `exit ${outcome.exit_code}` : 'error'}
              </span>
              <button type="button" className="template-member"
                onClick={(event) => {
                  event.preventDefault()
                  onOpen(instance.name)
                }}>
                {instance.name}
              </button>
              {!stdout && !stderr && !instance.error && <span className="faint">no output</span>}
            </summary>
            {(stdout || stderr || instance.error) && (
              <div className="console template-exec-output">
                {outcome?.truncated && <div className="dim">… earlier output cut</div>}
                {stdout && <div>{stdout}</div>}
                {stderr && <div className="err">{stderr}</div>}
                {instance.error && <div className="err">{instance.error}</div>}
              </div>
            )}
          </details>
        )
      })}
    </div>
  )
}

interface ExecProps {
  template: InstanceTemplate
  members: Container[]
  lastCommand: string
  onCancel: () => void
  onConfirm: (instances: string[], command: string, timeout: number) => void
}

/** Pick a command and which of a template's running instances it runs on. */
function ExecDialog({ template, members, lastCommand, onCancel, onConfirm }: ExecProps) {
  const [command, setCommand] = useState(lastCommand)
  const [timeout, setTimeoutSeconds] = useState('300')
  // Unticked rather than ticked, so an instance that starts while the dialog
  // is open is included without anyone having to notice it.
  const [excluded, setExcluded] = useState<Set<string>>(new Set())

  const running = members.filter((c) => c.status === 'Running')
  const targets = running.map((c) => c.name).filter((name) => !excluded.has(name))
  const seconds = Number.parseInt(timeout, 10)
  const timeoutValid = Number.isFinite(seconds) && seconds >= 1 && seconds <= 3600
  const canRun = command.trim().length > 0 && targets.length > 0 && timeoutValid

  function toggle(name: string) {
    setExcluded((current) => {
      const next = new Set(current)
      if (next.has(name)) next.delete(name)
      else next.add(name)
      return next
    })
  }

  return (
    <Modal
      title={`Run a command on “${template.name}”`}
      subtitle="Runs through sh -c on each selected instance at the same time, non-interactively, as root."
      onClose={onCancel}
      footer={
        <>
          <button type="button" className="btn" onClick={onCancel}>Cancel</button>
          <button type="submit" form="template-exec-form" className="btn btn-primary"
            disabled={!canRun}>
            Run on {targets.length}
          </button>
        </>
      }
    >
      <form id="template-exec-form" style={{ display: 'contents' }}
        onSubmit={(event) => {
          event.preventDefault()
          if (canRun) onConfirm(targets, command.trim(), seconds)
        }}>
        <div className="field">
          <label htmlFor="exec-command">Command</label>
          <textarea
            id="exec-command"
            className="input mono template-exec-command"
            value={command}
            rows={3}
            placeholder="apt-get update && apt-get -y upgrade"
            spellCheck={false}
            autoComplete="off"
            onChange={(event) => setCommand(event.target.value)}
            onKeyDown={(event) => {
              // Enter runs it, as in the console; Shift+Enter is a new line.
              if (event.key === 'Enter' && !event.shiftKey) {
                event.preventDefault()
                if (canRun) onConfirm(targets, command.trim(), seconds)
              }
            }}
          />
          <span className="hint">
            Enter runs it; Shift+Enter adds a line. There is no TTY, so anything
            that asks a question needs its non-interactive flag (<span className="mono">-y</span>).
          </span>
        </div>

        <div className="field">
          <label>Run on</label>
          {members.length === 0 ? (
            <p className="hint">No instances from this template remain.</p>
          ) : (
            <ul className="template-results">
              {members.map((c) => {
                const up = c.status === 'Running'
                return (
                  <li key={c.name}>
                    <label className="checkbox">
                      <input type="checkbox" disabled={!up}
                        checked={up && !excluded.has(c.name)}
                        onChange={() => toggle(c.name)} />
                      <span className="mono">{c.name}</span>
                    </label>
                    {up ? (
                      c.ipv4[0] && <span className="faint mono">{c.ipv4[0]}</span>
                    ) : (
                      <span className="faint">{c.status.toLowerCase()} — skipped</span>
                    )}
                  </li>
                )
              })}
            </ul>
          )}
        </div>

        <div className="field" style={{ maxWidth: 180 }}>
          <label htmlFor="exec-timeout">Timeout per instance (s)</label>
          <input id="exec-timeout" className="input num" type="number" min={1} max={3600}
            value={timeout} aria-invalid={!timeoutValid}
            onChange={(event) => setTimeoutSeconds(event.target.value)} />
        </div>
      </form>
    </Modal>
  )
}
