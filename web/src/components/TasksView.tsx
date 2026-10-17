import { useCallback, useEffect, useState } from 'react'
import { useCanOperate, useCanWrite } from '../hooks/useAuth'
import { api, calls } from '../lib/api'
import { syncDetail, syncKind } from '../lib/sync'
import { inputEnv, paramsReferring, secretEnv } from '../lib/tasks'
import type {
  BootstrapModule, InstanceTemplate, NodeGroup, Task, TaskRun, TaskRunModule, TaskRunStatus,
} from '../lib/types'
import { ConfirmDialog } from './ConfirmDialog'
import { KeyIcon, PauseIcon, PencilIcon, PlayIcon, PlusIcon, StopIcon, TrashIcon } from './Icons'
import { Modal } from './Modal'
import { HelpButton, HelpPanel } from './HelpTip'
import { TargetPicker } from './TargetPicker'
import { TaskEditor } from './TaskEditor'

interface Props {
  localNode: string
  onNotify: (kind: 'success' | 'error' | 'info', title: string, detail?: string) => void
}

// Every listing asks each member for its runs, so slower than App's own poll.
const RUNS_POLL = 5000

const STATUS_BADGE: Record<TaskRunStatus, string> = {
  running: 'badge-info', ok: 'badge-ok', failed: 'badge-danger', timed_out: 'badge-danger',
  cancelled: 'badge-dim', missed: 'badge-warn', vetoed: 'badge-warn', lost: 'badge-warn',
}

function when(epoch: number | null) {
  return epoch ? new Date(epoch * 1000).toLocaleString() : '—'
}

function took(run: TaskRun) {
  const seconds = Math.round((run.finished_at ?? Date.now() / 1000) - run.started_at)
  return seconds < 120 ? `${seconds}s` : `${Math.round(seconds / 60)} min`
}

/** What a run said last: its job's final line, else why it failed. */
function lastWords(run: TaskRun) {
  const modules = run.job?.modules ?? []
  const said = [...modules].reverse().map((m) => m.summary ?? m.stdout ?? '').find(Boolean)
  return said?.trim().split('\n').pop() ?? run.error ?? ''
}

/** Tasks: job modules in a throwaway instance, scheduled or invoked. See tasks.py. */
export function TasksView({ localNode, onNotify }: Props) {
  const canWrite = useCanWrite()
  const canOperate = useCanOperate()
  const [tasks, setTasks] = useState<Task[] | null>(null)
  const [templates, setTemplates] = useState<InstanceTemplate[]>([])
  const [modules, setModules] = useState<BootstrapModule[]>([])
  const [groups, setGroups] = useState<NodeGroup[]>([])
  const [members, setMembers] = useState<string[]>([])
  const [runs, setRuns] = useState<TaskRun[]>([])
  const [runErrors, setRunErrors] = useState<{ node: string; error: string }[]>([])
  const [editing, setEditing] = useState<Task | 'new' | null>(null)
  const [invoking, setInvoking] = useState<Task | null>(null)
  const [settingSecret, setSettingSecret] = useState<{ task: Task; secret: string } | null>(null)
  const [pendingDelete, setPendingDelete] = useState<string | null>(null)
  const [showing, setShowing] = useState<TaskRun | null>(null)
  const [filter, setFilter] = useState<string | null>(null)

  const load = useCallback(() => {
    api.tasks().then(setTasks).catch((cause) => {
      setTasks([])
      onNotify('error', 'Could not load tasks', (cause as Error).message)
    })
  }, [onNotify])

  useEffect(() => {
    load()
    api.templates().then(setTemplates).catch(() => {})
    api.modules().then(setModules).catch(() => {})
    // Without a cluster there are no groups to offer, and this node is the
    // only member; the editor then says to make a group.
    api.nodeGroups().then(setGroups).catch(() => setGroups([]))
    api.nodes(undefined, false).then((found) => setMembers(found.map((n) => n.name)))
      .catch(() => setMembers(localNode ? [localNode] : []))
  }, [load, localNode])

  useEffect(() => {
    let stopped = false
    const controller = new AbortController()
    const poll = () => {
      api.taskRuns(undefined, controller.signal).then((found) => {
        if (stopped) return
        setRuns(found.runs)
        setRunErrors(found.errors)
      }).catch(() => {})
    }
    poll()
    const timer = window.setInterval(poll, RUNS_POLL)
    return () => { stopped = true; controller.abort(); window.clearInterval(timer) }
  }, [])

  const refreshRuns = () => {
    api.taskRuns().then((found) => { setRuns(found.runs); setRunErrors(found.errors) })
      .catch(() => {})
  }

  async function toggle(task: Task) {
    try {
      const saved = await api.setTaskEnabled(task.name, !task.enabled)
      onNotify(syncKind(saved.synced), `${saved.enabled ? 'Resumed' : 'Paused'} ${task.name}`,
        syncDetail(saved.synced))
      load()
    } catch (cause) {
      onNotify('error', `Could not change ${task.name}`, (cause as Error).message)
    }
  }

  async function remove(name: string) {
    try {
      const result = await api.deleteTask(name)
      onNotify(syncKind(result.synced), `Deleted task ${name}`, syncDetail(result.synced))
      load()
    } catch (cause) {
      onNotify('error', `Could not delete ${name}`, (cause as Error).message)
    } finally {
      setPendingDelete(null)
    }
  }

  async function cancel(run: TaskRun) {
    try {
      await api.cancelTaskRun(run.id, run.node === localNode ? undefined : run.node)
      onNotify('info', `Cancelling ${run.task}`, `Run ${run.id} on ${run.node}`)
      refreshRuns()
    } catch (cause) {
      onNotify('error', 'Could not cancel the run', (cause as Error).message)
    }
  }

  async function open(run: TaskRun) {
    try {
      setShowing(await api.taskRun(run.id, run.node === localNode ? undefined : run.node))
    } catch (cause) {
      onNotify('error', 'Could not load the run', (cause as Error).message)
    }
  }

  const shownRuns = filter ? runs.filter((r) => r.task === filter) : runs

  return (
    <>
      <div className="section-head">
        <h2>Tasks</h2>
        <span className="faint" style={{ fontSize: 12.5 }}>
          job modules in a throwaway instance, on a schedule or when invoked
        </span>
        <div className="topbar-spacer" />
        <button className="btn btn-primary btn-sm" disabled={!canWrite}
          onClick={() => setEditing('new')}>
          <PlusIcon /> New task
        </button>
      </div>

      {tasks === null ? (
        <div className="card">
          <div className="loading-wrap"><span className="spinner" /> Loading tasks…</div>
        </div>
      ) : tasks.length === 0 ? (
        <div className="card">
          <div className="empty">
            <h3>No tasks yet</h3>
            <p>
              A task is a template plus job modules: every run gets a fresh instance, runs the
              job, and destroys it. Run one on a schedule — the cluster decides which node,
              each time — or invoke it with inputs, like a message to a function.
            </p>
          </div>
        </div>
      ) : (
        <div className="template-grid">
          {tasks.map((task) => {
            const last = runs.find((r) => r.task === task.name && r.status !== 'running')
            const going = runs.filter((r) => r.task === task.name && r.status === 'running')
            const unset = Object.keys(task.secrets).filter((s) => !task.secret_status[s])
            return (
              <section className="card template-card" key={task.name}>
                <header className="module-card-head">
                  <div className="module-card-title">
                    <h3>{task.name}</h3>
                    {task.schedule && !task.enabled && <span className="vm-tag">PAUSED</span>}
                    {going.length > 0 && (
                      <span className="badge badge-info">{going.length} running</span>
                    )}
                  </div>
                  <button className="btn btn-sm" disabled={!canOperate}
                    onClick={() => setInvoking(task)}>
                    <PlayIcon /> Run
                  </button>
                  {task.schedule && (
                    <button className="btn btn-sm btn-icon btn-ghost" disabled={!canWrite}
                      aria-label={task.enabled ? 'Pause schedule' : 'Resume schedule'}
                      title={task.enabled ? 'Pause schedule' : 'Resume schedule'}
                      onClick={() => toggle(task)}>
                      {task.enabled ? <PauseIcon /> : <PlayIcon />}
                    </button>
                  )}
                  <button className="btn btn-sm btn-icon btn-ghost" disabled={!canWrite}
                    aria-label={`Edit task ${task.name}`} title="Edit"
                    onClick={() => setEditing(task)}>
                    <PencilIcon />
                  </button>
                  <button className="btn btn-sm btn-icon btn-danger" disabled={!canWrite}
                    aria-label={`Delete task ${task.name}`} title="Delete task"
                    onClick={() => setPendingDelete(task.name)}>
                    <TrashIcon />
                  </button>
                </header>
                <div className="module-card-body">
                  {task.description && <p className="module-desc-full">{task.description}</p>}
                  <dl className="kv">
                    <dt>Runs</dt>
                    <dd>
                      {task.schedule ? (
                        <>
                          {task.schedule_text ?? task.schedule.cron}
                          <span className="faint"> · {task.schedule.timezone}</span>
                          {task.next_run && task.enabled && (
                            <span className="faint"> · next {when(task.next_run)}</span>
                          )}
                        </>
                      ) : <span className="faint">when invoked</span>}
                      {task.webhook && (
                        <span className="faint">
                          {' '}· per queued GitHub job at{' '}
                          <span className="mono">{task.hook_path}</span>
                        </span>
                      )}
                      {task.schedule_problem && (
                        <div className="err">{task.schedule_problem}</div>
                      )}
                    </dd>
                    <dt>Template</dt>
                    <dd>{task.template}</dd>
                    <dt>Job</dt>
                    <dd>
                      {task.script ? (
                        <details className="stack-run-output" style={{ paddingLeft: 0 }}>
                          <summary>
                            script · {task.script.trimEnd().split('\n').length} lines
                          </summary>
                          <pre>{task.script}</pre>
                        </details>
                      ) : <span className="mono">{task.bootstrap.modules.join(' → ')}</span>}
                    </dd>
                    {Object.keys(task.inputs).length > 0 && (
                      <>
                        <dt>Inputs</dt>
                        <dd className="mono">
                          {Object.entries(task.inputs).map(([n, spec]) => (
                            `${n}${spec.required ? '*' : ''}${spec.secret ? ' (secret)' : ''}`
                          )).join(', ')}
                        </dd>
                      </>
                    )}
                    {Object.keys(task.secrets).length > 0 && (
                      <>
                        <dt>Secrets</dt>
                        <dd>
                          {Object.keys(task.secrets).map((s) => (
                            <button key={s} className="btn btn-sm" disabled={!canWrite}
                              title="Set its value" style={{ marginRight: 6 }}
                              onClick={() => setSettingSecret({ task, secret: s })}>
                              <KeyIcon /> {s}
                              {!task.secret_status[s] && (
                                <span className="badge badge-warn">not set</span>
                              )}
                            </button>
                          ))}
                        </dd>
                      </>
                    )}
                    <dt>Runs on</dt>
                    <dd>
                      {[...task.groups.map((g) => `group ${g}`), ...task.nodes].join(', ')
                        || <span className="err">nothing</span>}
                      {task.eligible.length > 0 && (
                        <span className="faint"> ({task.eligible.join(', ')})</span>
                      )}
                      {task.placement_problem && (
                        <div className="err">{task.placement_problem}</div>
                      )}
                      <span className="faint">
                        {' '}· {task.timeout_seconds}s timeout
                        {task.overlap === 'allow' ? ' · overlapping runs allowed' : ''}
                      </span>
                    </dd>
                    {last && (
                      <>
                        <dt>Last run</dt>
                        <dd>
                          <button className="btn btn-sm btn-ghost" onClick={() => open(last)}>
                            <span className={`badge ${STATUS_BADGE[last.status]}`}>
                              {last.status.replace('_', ' ')}
                            </span>
                            {' '}{when(last.started_at)} on {last.node}
                          </button>
                        </dd>
                      </>
                    )}
                  </dl>
                  {unset.length > 0 && (
                    <p className="hint" style={{ color: 'var(--warn)' }}>
                      {unset.join(', ')} {unset.length === 1 ? 'holds' : 'hold'} no value yet;
                      runs needing {unset.length === 1 ? 'it' : 'them'} will fail.
                    </p>
                  )}
                </div>
              </section>
            )
          })}
        </div>
      )}

      <div className="section-head" style={{ marginTop: 20 }}>
        <h2>Runs</h2>
        <span className="faint" style={{ fontSize: 12.5 }}>
          on every member, newest first{filter ? ` · ${filter} only` : ''}
        </span>
        <div className="topbar-spacer" />
        {tasks && tasks.length > 1 && (
          <select className="input" style={{ width: 'auto' }} value={filter ?? ''}
            aria-label="Show runs of" onChange={(e) => setFilter(e.target.value || null)}>
            <option value="">All tasks</option>
            {tasks.map((t) => <option key={t.name} value={t.name}>{t.name}</option>)}
          </select>
        )}
      </div>
      {runErrors.length > 0 && (
        <div className="banner banner-warn">
          <div className="banner-body">
            {runErrors.map((e) => `${e.node} did not answer: ${e.error}`).join('; ')}
          </div>
        </div>
      )}
      <div className="card">
        {shownRuns.length === 0 ? (
          <div className="empty"><p>No runs yet.</p></div>
        ) : (
          <div className="table-scroll">
            <table className="ctable">
              <thead>
                <tr>
                  <th>Status</th><th>Task</th><th>Node</th><th>Trigger</th><th>Started</th>
                  <th>Took</th><th>Said</th><th />
                </tr>
              </thead>
              <tbody>
                {shownRuns.map((run) => (
                  <tr key={`${run.node}/${run.id}`} onClick={() => open(run)}>
                    <td>
                      <span className={`badge ${STATUS_BADGE[run.status]}`}>
                        {run.status === 'running' ? run.stage : run.status.replace('_', ' ')}
                      </span>
                      {run.exit_code ? <span className="faint"> exit {run.exit_code}</span> : null}
                    </td>
                    <td>{run.task}</td>
                    <td>{run.node}</td>
                    <td className="faint">
                      {run.trigger === 'schedule' ? `schedule · ${when(run.slot)}` : 'invoked'}
                    </td>
                    <td>{when(run.started_at)}</td>
                    <td>{took(run)}</td>
                    <td className="truncate mono" style={{ maxWidth: 360 }}>{lastWords(run)}</td>
                    <td>
                      {run.status === 'running' && (
                        <button className="btn btn-sm btn-danger" disabled={!canOperate}
                          onClick={(e) => { e.stopPropagation(); cancel(run) }}>
                          <StopIcon /> Cancel
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {editing && (
        <TaskEditor
          task={editing === 'new' ? null : editing}
          templates={templates}
          modules={modules}
          groups={groups}
          nodes={members}
          onCancel={() => setEditing(null)}
          onSaved={(saved) => {
            setEditing(null)
            onNotify(syncKind(saved.synced), `Saved task ${saved.name}`,
              [syncDetail(saved.synced), ...saved.warnings].filter(Boolean).join(' '))
            load()
          }}
        />
      )}
      {invoking && (
        <InvokeDialog task={invoking} onCancel={() => setInvoking(null)}
          onStarted={(run) => {
            setInvoking(null)
            onNotify('info', `Started ${run.task}`, `Run ${run.id} on ${run.node}`)
            refreshRuns()
          }} />
      )}
      {settingSecret && (
        <SecretDialog task={settingSecret.task} secret={settingSecret.secret}
          onCancel={() => setSettingSecret(null)}
          onDone={(message) => {
            setSettingSecret(null)
            onNotify('success', message)
            load()
          }} />
      )}
      {pendingDelete && (
        <ConfirmDialog title={`Delete task ${pendingDelete}?`}
          message="It is removed from every member and stops being scheduled. Runs going now
            finish; their history stays."
          confirmLabel="Delete task" danger onConfirm={() => remove(pendingDelete)}
          onCancel={() => setPendingDelete(null)} />
      )}
      {showing && <RunDialog run={showing} onClose={() => setShowing(null)} />}
    </>
  )
}

/** Run now: the task's inputs, a payload, and optionally where. */
function InvokeDialog({ task, onCancel, onStarted }: {
  task: Task
  onCancel: () => void
  onStarted: (run: TaskRun) => void
}) {
  const [values, setValues] = useState<Record<string, string>>({})
  const [payload, setPayload] = useState('')
  const [node, setNode] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [help, setHelp] = useState(false)
  const missing = Object.entries(task.inputs)
    .filter(([n, spec]) => spec.required && !spec.default && !values[n]).map(([n]) => n)

  // A payload that parses as JSON goes as JSON; anything else as the text it is.
  function parsedPayload(): unknown {
    if (!payload.trim()) return undefined
    try { return JSON.parse(payload) } catch { return payload }
  }
  const inputs = Object.fromEntries(Object.entries(values).filter(([, v]) => v !== ''))

  async function start() {
    setBusy(true)
    setError(null)
    try {
      onStarted(await api.invokeTask(task.name, inputs, parsedPayload(), node || undefined))
    } catch (cause) {
      setError((cause as Error).message)
      setBusy(false)
    }
  }

  return (
    <Modal title={`Run “${task.name}”`}
      subtitle="Starts now on whichever member takes it, in a fresh instance."
      onClose={busy ? () => {} : onCancel}
      api={{ ...calls.invokeTask(task.name, inputs, parsedPayload(), node || undefined),
        secrets: Object.entries(task.inputs).filter(([, s]) => s.secret)
          .map(([n]) => `inputs.${n}`) }}
      footer={
        <>
          <button className="btn" onClick={onCancel} disabled={busy}>Cancel</button>
          <button className="btn btn-primary"
            disabled={busy || missing.length > 0 || task.eligible.length === 0}
            onClick={start}>
            {busy && <span className="spinner" />} Run
          </button>
        </>
      }
    >
      <p className="hint" style={{ marginTop: 0 }}>
        {Object.keys(task.inputs).length
          ? 'These are read when the run starts; one left empty takes its default.'
          : 'This task declares no inputs; a payload is still handed over.'}
        <HelpButton open={help} onToggle={() => setHelp((open) => !open)}
          label="How these reach the job" />
      </p>
      {help && <InvokeHelp task={task} values={values} hasPayload={!!payload.trim()}
        node={node} onClose={() => setHelp(false)} />}
      {Object.entries(task.inputs).map(([name, spec]) => (
        <div className="field" key={name}>
          <label htmlFor={`in-${name}`}>
            {name}
            {spec.secret && <span className="badge badge-warn">secret</span>}
            {spec.required && <span className="badge badge-dim">required</span>}
          </label>
          <input id={`in-${name}`} className="input mono" value={values[name] ?? ''}
            type={spec.secret ? 'password' : 'text'}
            autoComplete={spec.secret ? 'new-password' : 'off'}
            placeholder={spec.default || (spec.required ? '' : 'empty')}
            onChange={(e) => setValues((v) => ({ ...v, [name]: e.target.value }))} />
          <span className="hint">
            {spec.description}{spec.description && ' · '}
            <span className="faint">
              the job reads it as <span className="mono">${inputEnv(name)}</span>
              {!task.script && paramsReferring(task.bootstrap.params, 'inputs', name).length > 0
                && <> and in <span className="mono">
                  {paramsReferring(task.bootstrap.params, 'inputs', name).join(', ')}</span></>}
              {spec.default && <> · empty means <span className="mono">{spec.default}</span></>}
              {spec.secret && ' · masked, not kept'}
            </span>
          </span>
        </div>
      ))}
      <div className="field">
        <label htmlFor="in-payload">Payload</label>
        <textarea id="in-payload" className="input mono param-text" rows={5}
          value={payload} spellCheck={false} onChange={(e) => setPayload(e.target.value)}
          placeholder='{"message": "…"}  — optional, JSON or text' />
        <span className="hint">
          Handed to the job as a file named by <span className="mono">$WATERPUMP_TASK_PAYLOAD</span>;
          never logged or kept.
        </span>
      </div>
      <div className="field">
        <label htmlFor="in-node">Node</label>
        <TargetPicker id="in-node" single groups={[]} nodes={task.eligible}
          value={{ groups: [], nodes: node ? [node] : [] }}
          onChange={(next) => setNode(next.nodes[0] ?? '')}
          placeholder="let the cluster choose — or type a member of its groups" />
        <span className="hint">
          Runs on {task.groups.map((g) => `group ${g}`).join(', ')}
          {task.eligible.length ? ` (${task.eligible.join(', ')})` : ' — which has no members'}.
        </span>
      </div>
      {error && <div className="err">{error}</div>}
    </Modal>
  )
}

/** Where the values in the run dialog go, and the same run from the CLI. */
function InvokeHelp({ task, values, hasPayload, node, onClose }: {
  task: Task
  values: Record<string, string>
  hasPayload: boolean
  node: string
  onClose: () => void
}) {
  const entries = Object.entries(task.inputs)
  // The command for exactly this run: plain inputs as flags, secret ones from
  // the environment, which is how the CLI wants them.
  const flags = entries.filter(([n, s]) => !s.secret && values[n])
    .map(([n]) => ` --input ${n}=${JSON.stringify(values[n])}`).join('')
  const env = entries.filter(([, s]) => s.secret).map(([n]) => `${n}=… `).join('')
  const command = `${env}waterpump task-run ${task.name}${flags}`
    + `${hasPayload ? ' --payload message.json' : ''}${node ? ` --node ${node}` : ''}`
  return (
    <HelpPanel title="How these reach the job" onClose={onClose}>
      <table className="help-table">
        <thead>
          <tr><th>Input</th><th>The job reads it as</th><th>If left empty</th></tr>
        </thead>
        <tbody>
          {entries.map(([n, spec]) => {
            const users = task.script ? [] : paramsReferring(task.bootstrap.params, 'inputs', n)
            return (
              <tr key={n}>
                <td className="mono">{n}</td>
                <td className="mono">
                  ${inputEnv(n)}{users.length > 0 && <>, {users.join(', ')}</>}
                </td>
                <td>{spec.default ? <span className="mono">{spec.default}</span>
                  : spec.required ? 'refused: required' : 'empty'}</td>
              </tr>
            )
          })}
          <tr>
            <td>payload</td>
            <td className="mono">the file at $WATERPUMP_TASK_PAYLOAD</td>
            <td>no file, and no variable</td>
          </tr>
        </tbody>
      </table>
      <p>
        Secret inputs are masked in output and never kept. The payload is never logged or
        kept either — the run&rsquo;s history shows only the plain inputs and the
        payload&rsquo;s size.
        {Object.keys(task.secrets).length > 0 && <> The task&rsquo;s own secrets (
          {Object.keys(task.secrets).join(', ')}) are added by the server; nobody running it
          sees or gives them.</>}
      </p>
      <p>The same run from the CLI:</p>
      <pre>{command}</pre>
      <p className="faint">
        The API call this dialog makes is under <span className="mono">&lt;/&gt; API</span>, for
        calling it from a webhook with an operator token.
      </p>
    </HelpPanel>
  )
}

function SecretDialog({ task, secret, onCancel, onDone }: {
  task: Task
  secret: string
  onCancel: () => void
  onDone: (message: string) => void
}) {
  const [value, setValue] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const held = !!task.secret_status[secret]
  const users = paramsReferring(task.bootstrap.params, 'secrets', secret)

  async function act(clear: boolean) {
    setBusy(true)
    setError(null)
    try {
      if (clear) await api.clearTaskSecret(task.name, secret)
      else await api.setTaskSecret(task.name, secret, value)
      onDone(`${clear ? 'Cleared' : 'Set'} ${secret} of ${task.name}`)
    } catch (cause) {
      setError((cause as Error).message)
      setBusy(false)
    }
  }

  return (
    <Modal title={`Secret ${secret}`} subtitle={task.secrets[secret]?.description
      || `Sealed on every member, handed to ${task.name}'s job.`}
      onClose={busy ? () => {} : onCancel}
      footer={
        <>
          {held && (
            <button className="btn btn-danger" disabled={busy} onClick={() => act(true)}>
              Clear
            </button>
          )}
          <button className="btn" onClick={onCancel} disabled={busy}>Cancel</button>
          <button className="btn btn-primary" disabled={busy || !value} onClick={() => act(false)}>
            Save
          </button>
        </>
      }
    >
      <div className="field">
        <label htmlFor="secret-value">{held ? 'New value' : 'Value'}</label>
        <input id="secret-value" className="input mono" type="password"
          autoComplete="new-password" value={value} onChange={(e) => setValue(e.target.value)} />
        <span className="hint">
          {task.script ? (
            <>The script reads it as <span className="mono">${secretEnv(secret)}</span>.</>
          ) : users.length ? (
            <>Handed to the job through{' '}
              <span className="mono">{users.join(', ')}</span>.</>
          ) : (
            <span style={{ color: 'var(--warn)' }}>
              No parameter of the job names it yet: set one to{' '}
              <span className="mono">{`{{secrets.${secret}}}`}</span> in the task.
            </span>
          )}{' '}
          Sealed on every member and masked in output; it cannot be shown again, only
          replaced or cleared.
        </span>
      </div>
      {error && <div className="err">{error}</div>}
    </Modal>
  )
}

function ModuleOutput({ part, modules }: { part: string; modules: TaskRunModule[] }) {
  return (
    <>
      {modules.map((m, index) => {
        const text = `${m.stdout ?? m.summary ?? ''}${m.stderr ?? ''}`.trimEnd()
        return (
          <details className="stack-run-output" key={`${part}-${index}`}
            open={part === 'job' || m.exit_code !== 0}>
            <summary>
              {part} · {m.name}{' '}
              <span className={`badge ${m.exit_code ? 'badge-danger' : 'badge-ok'}`}>
                exit {m.exit_code}
              </span>
              <span className="faint"> · {m.duration}s</span>
            </summary>
            {text ? <pre>{text}</pre> : <p className="faint">no output</p>}
          </details>
        )
      })}
    </>
  )
}

function RunDialog({ run, onClose }: { run: TaskRun; onClose: () => void }) {
  return (
    <Modal title={`${run.task} · run ${run.id}`}
      subtitle={`${run.trigger === 'schedule' ? `Scheduled for ${when(run.slot)}` : 'Invoked'}`
        + ` · on ${run.node} · ${when(run.started_at)} · ${took(run)}`}
      onClose={onClose}
      footer={<button className="btn" onClick={onClose}>Close</button>}>
      <dl className="kv">
        <dt>Status</dt>
        <dd>
          <span className={`badge ${STATUS_BADGE[run.status]}`}>
            {run.status.replace('_', ' ')}
          </span>
          {run.exit_code !== null && <span className="faint"> · exit {run.exit_code}</span>}
        </dd>
        {run.instance && (<><dt>Instance</dt><dd className="mono">{run.instance}</dd></>)}
        {Object.keys(run.inputs).length > 0 && (
          <>
            <dt>Inputs</dt>
            <dd className="mono">
              {Object.entries(run.inputs).map(([k, v]) => `${k}=${v}`).join('  ')}
            </dd>
          </>
        )}
        {run.payload_bytes > 0 && (<><dt>Payload</dt><dd>{run.payload_bytes} bytes</dd></>)}
        {run.error && (<><dt>Error</dt><dd className="err">{run.error}</dd></>)}
        {run.notes.length > 0 && (<><dt>Notes</dt><dd>{run.notes.join(' ')}</dd></>)}
      </dl>
      <ModuleOutput part="setup" modules={run.setup?.modules ?? []} />
      <ModuleOutput part="job" modules={run.job?.modules ?? []} />
    </Modal>
  )
}
