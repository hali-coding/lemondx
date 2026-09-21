import { useCallback, useEffect, useState } from 'react'
import { useCanWrite } from '../hooks/useAuth'
import { api } from '../lib/api'
import { syncDetail, syncKind } from '../lib/sync'
import type {
  BootstrapModule, ClusterNode, InstanceTemplate, NodeGroup, Stack, StackInstance,
  StackInstances, StackRun, StackStep, StackStepRun,
} from '../lib/types'
import { ConfirmDialog } from './ConfirmDialog'
import {
  ClockIcon, CloseIcon, HeartIcon, PencilIcon, PlayIcon, PlusIcon, RestartIcon, ServerIcon,
  StopIcon, TrashIcon,
} from './Icons'
import { Modal } from './Modal'
import { StackEditor } from './StackEditor'
import { StatusBadge } from './StatusBadge'

interface Props {
  localNode: string
  /** Whether the daemon can create instances yet; designing works regardless. */
  ready: boolean
  /** What the server is running or last ran per stack, from App's poll. */
  runs: StackRun[]
  onRunStarted: (run: StackRun) => void
  onNotify: (kind: 'success' | 'error' | 'info', title: string, detail?: string) => void
  onOpen: (name: string, node?: string) => void
  /** Refresh App's poll now. */
  onChanged: () => void
}

type Param = BootstrapModule['params'][number]

// Every listing asks each node, so this is slower than App's own poll.
const INSTANCE_POLL = 5000

/** One value asked for at launch: a module's secret, or a `{{params.NAME}}` input. */
type Ask = {
  name: string
  secret: boolean
  description: string
  /** Where it lands: `step.PARAM` for each launch that takes this value. */
  goes: string[]
}

/**
 * Which step parameters each `{{params.NAME}}` fills in.
 *
 * The dialog used to say only "used by a step of this stack", which left the
 * person typing a password with no way to tell what it was for. A value that
 * is about to be handed to three launches should say so.
 */
function inputTargets(stack: Stack): Map<string, string[]> {
  const found = new Map<string, string[]>()
  for (const step of stack.stages.flatMap((s) => s.steps)) {
    if (step.type !== 'launch') continue
    for (const [param, value] of Object.entries(step.params)) {
      for (const match of value.matchAll(/\{\{\s*params\.([A-Za-z_][A-Za-z0-9_]{0,63})\s*\}\}/g)) {
        found.set(match[1], [...(found.get(match[1]) ?? []), `${step.id}.${param}`])
      }
    }
  }
  return found
}

/**
 * What a launch of the stack has to be given: the secrets its templates' modules
 * need, and whatever its steps refer to as `{{params.NAME}}` -- a password
 * typed once and handed to every step that asks. Each is asked for once and
 * shared by every launch in the stack.
 */
function stackAsks(stack: Stack, templates: InstanceTemplate[], modules: BootstrapModule[]): Ask[] {
  const targets = inputTargets(stack)
  const found = new Map<string, Ask>(
    stackSecrets(stack, templates, modules).map((p) => [p.name, {
      name: p.name, secret: true, description: p.description, goes: targets.get(p.name) ?? [] }]))
  const secretNames = new Set(modules.flatMap((m) => m.params).filter((p) => p.secret)
    .map((p) => p.name))
  // A declared parameter of the same name is the best description there is for
  // an input; failing that the name is all anyone wrote down.
  const declared = new Map(modules.flatMap((m) => m.params).map((p) => [p.name, p.description]))
  for (const input of stack.inputs) {
    if (!found.has(input)) {
      found.set(input, {
        name: input,
        secret: secretNames.has(input) || /pass|secret|token|key/i.test(input),
        description: declared.get(input) ?? '',
        goes: targets.get(input) ?? [],
      })
    }
  }
  return [...found.values()]
}

const refOf = (i: StackInstance) => ({ node: i.node, name: i.name })

/** Secrets any launch in the stack needs; asked for once, shared by all of them. */
function stackSecrets(stack: Stack, templates: InstanceTemplate[],
                      modules: BootstrapModule[]) {
  const found = new Map<string, Param>()
  for (const step of stack.stages.flatMap((s) => s.steps)) {
    if (step.type !== 'launch') continue
    const used = templates.find((t) => t.name === step.template)?.bootstrap.modules ?? []
    for (const module of modules) {
      if (!used.includes(module.id)) continue
      // One the step fills in itself, from {{params.NAME}}, is not asked for.
      for (const param of module.params) {
        if (param.secret && !(param.name in step.params)) found.set(param.name, param)
      }
    }
  }
  return [...found.values()]
}

function launches(stack: Stack) {
  return stack.stages.flatMap((s) => s.steps).filter((s) => s.type === 'launch')
}

function remaining(until: number | null) {
  if (!until) return ''
  const left = Math.max(0, Math.round(until - Date.now() / 1000))
  return left >= 120 ? `${Math.round(left / 60)} min left` : `${left}s left`
}

/** Sequences of template launches, pauses and health gates, run with one click. */
export function StacksView({ localNode, ready, runs: runList, onRunStarted, onNotify, onOpen,
                             onChanged }: Props) {
  const canWrite = useCanWrite()
  const [stacks, setStacks] = useState<Stack[] | null>(null)
  const [templates, setTemplates] = useState<InstanceTemplate[]>([])
  const [modules, setModules] = useState<BootstrapModule[]>([])
  const [nodes, setNodes] = useState<ClusterNode[]>([])
  const [groups, setGroups] = useState<NodeGroup[]>([])
  const [error, setError] = useState<string | null>(null)
  const [editing, setEditing] = useState<Stack | 'new' | null>(null)
  const [pendingDelete, setPendingDelete] = useState<string | null>(null)
  const [deleteEverywhere, setDeleteEverywhere] = useState(true)
  const [asking, setAsking] = useState<{ stack: Stack; relaunch: boolean } | null>(null)
  const [destroying, setDestroying] = useState<Stack | null>(null)
  const [instances, setInstances] = useState<StackInstances | null>(null)
  const [acting, setActing] = useState<Record<string, string>>({})
  const [starting, setStarting] = useState<Record<string, boolean>>({})
  // Re-render every second while something counts down.
  const [, setTick] = useState(0)

  const load = useCallback((signal?: AbortSignal) => {
    Promise.all([api.stacks(signal), api.templates(signal), api.modules(signal)])
      .then(([loadedStacks, loadedTemplates, loadedModules]) => {
        setStacks(loadedStacks)
        setTemplates(loadedTemplates)
        setModules(loadedModules)
        setError(null)
      })
      .catch((cause) => {
        if ((cause as Error).name !== 'AbortError') setError((cause as Error).message)
      })
    Promise.all([api.nodes(signal, false), api.nodeGroups(signal)])
      .then(([loadedNodes, loadedGroups]) => {
        setNodes(loadedNodes)
        setGroups(loadedGroups)
      })
      .catch(() => {})
  }, [])

  useEffect(() => {
    const controller = new AbortController()
    load(controller.signal)
    return () => controller.abort()
  }, [load])

  const loadInstances = useCallback((signal?: AbortSignal) => {
    api.stackInstances(signal).then(setInstances).catch(() => {})
  }, [])

  // The instances are the stack's state: read from their tags on every node,
  // so a stack launched from another node's UI, or before a restart, shows too.
  useEffect(() => {
    const controller = new AbortController()
    loadInstances(controller.signal)
    const timer = window.setInterval(() => loadInstances(), INSTANCE_POLL)
    return () => {
      controller.abort()
      window.clearInterval(timer)
    }
  }, [loadInstances])

  const runs = Object.fromEntries(runList.map((run) => [run.stack, run]))
  const counting = runList.some((r) => r.finished_at === null)
  useEffect(() => {
    if (!counting) return
    const timer = window.setInterval(() => setTick((t) => t + 1), 1000)
    return () => window.clearInterval(timer)
  }, [counting])

  const membersOf = (stack: Stack) => instances?.stacks[stack.name] ?? []
  // By name, for the two moments a definition goes but its instances do not:
  // deleting it, and renaming it (which is a save under the new name and a
  // delete of the old).
  const orphansOf = (name: string) => instances?.stacks[name] ?? []

  async function launch(stack: Stack, params: Record<string, string>,
                        replace?: StackInstance[]) {
    setStarting((current) => ({ ...current, [stack.name]: true }))
    try {
      onRunStarted(await api.launchStack(stack.name, params, replace?.map(refOf)))
      window.setTimeout(() => loadInstances(), 1500)
    } catch (cause) {
      onNotify('error', `Could not ${replace ? 'relaunch' : 'launch'} “${stack.name}”`,
        (cause as Error).message)
    } finally {
      setStarting((current) => {
        const next = { ...current }
        delete next[stack.name]
        return next
      })
    }
  }

  function requestLaunch(stack: Stack, relaunch = false) {
    // A relaunch always asks: it destroys what is there, which is worth a look.
    if (relaunch || stackAsks(stack, templates, modules).length > 0) {
      setAsking({ stack, relaunch })
    } else {
      launch(stack, {})
    }
  }

  async function setState(stack: Stack, action: 'start' | 'stop') {
    setActing((current) => ({ ...current, [stack.name]: action }))
    try {
      const result = await api.stackState(stack.name, action, membersOf(stack).map(refOf))
      const failed = result.instances.filter((i) => !i.ok)
      if (failed.length) {
        onNotify('error', `${failed.length} of “${stack.name}” could not ${action}`,
          failed.map((i) => `${i.name}: ${i.error}`).join('; '))
      } else {
        onNotify('success', `${action === 'start' ? 'Started' : 'Stopped'} “${stack.name}”`,
          `${result.instances.length} instance(s).`)
      }
    } catch (cause) {
      onNotify('error', `Could not ${action} “${stack.name}”`, (cause as Error).message)
    } finally {
      setActing((current) => {
        const next = { ...current }
        delete next[stack.name]
        return next
      })
      loadInstances()
      onChanged()
    }
  }

  async function destroy(stack: Stack, members: StackInstance[]) {
    try {
      onRunStarted(await api.destroyStack(stack.name, members.map(refOf)))
      window.setTimeout(() => loadInstances(), 1500)
    } catch (cause) {
      onNotify('error', `Could not destroy “${stack.name}”`, (cause as Error).message)
    }
  }

  async function cancel(name: string) {
    try {
      await api.cancelStackRun(name)
      onNotify('info', `Cancelling “${name}”`,
        'Nothing new starts; launches already under way finish.')
    } catch (cause) {
      onNotify('error', 'Could not cancel', (cause as Error).message)
    }
    onChanged()
  }

  async function dismiss(name: string) {
    try {
      await api.dismissStackRun(name)
    } catch { /* already gone; the poll will tell */ }
    onChanged()
  }

  async function remove() {
    const name = pendingDelete
    if (!name) return
    try {
      const result = await api.deleteStack(name, deleteEverywhere)
      onNotify(syncKind(result.synced), `Deleted stack “${name}”`, syncDetail(result.synced))
      load()
    } catch (cause) {
      onNotify('error', 'Could not delete the stack', (cause as Error).message)
    } finally {
      setPendingDelete(null)
      setDeleteEverywhere(true)
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
        <h2>Stacks</h2>
        <span className="faint" style={{ fontSize: 12.5 }}>
          templates launched in sequence, with pauses and health gates between
        </span>
        <div className="topbar-spacer" />
        <button className="btn btn-primary btn-sm" disabled={!canWrite}
          onClick={() => setEditing('new')}>
          <PlusIcon /> New stack
        </button>
      </div>

      {stacks === null ? (
        <div className="card">
          <div className="loading-wrap"><span className="spinner" /> Loading stacks…</div>
        </div>
      ) : stacks.length === 0 ? (
        <div className="card">
          <div className="empty">
            <h3>No stacks yet</h3>
            <p>
              A stack launches several templates in order at the click of a button — one
              database, then three app servers pointed at it, then six load generators
              pointed at those. Later launches get the names and addresses of earlier ones.
            </p>
            <button className="btn btn-primary" disabled={!canWrite || templates.length === 0}
              onClick={() => setEditing('new')}>
              <PlusIcon /> Design a stack
            </button>
            {templates.length === 0 && (
              <p className="hint" style={{ marginTop: 10 }}>Make a template first.</p>
            )}
          </div>
        </div>
      ) : (
        <div className="stack-list">
          {stacks.map((stack) => {
            const run = runs[stack.name]
            const running = !!run && run.finished_at === null
            const busy = running || !!starting[stack.name] || !!acting[stack.name]
            const members = membersOf(stack)
            const up = members.filter((m) => m.status === 'Running').length
            const spread = new Set(members.map((m) => m.node)).size > 1
              || members.some((m) => m.node !== localNode)
            const missing = launches(stack).filter((s) => s.type === 'launch'
              && !templates.some((t) => t.name === s.template))
            const total = launches(stack).reduce(
              (sum, s) => sum + (s.type === 'launch' ? s.count : 0), 0)
            return (
              <section className="card stack-card" key={stack.name}>
                <header className="module-card-head">
                  <div className="module-card-title">
                    <h3>{stack.name}</h3>
                    <span className="badge badge-dim">
                      {stack.stages.length} stage{stack.stages.length === 1 ? '' : 's'}
                    </span>
                    <span className="badge badge-dim">{total} instance{total === 1 ? '' : 's'}</span>
                    {run && <RunBadge run={run} />}
                  </div>
                  <button className="btn btn-sm btn-icon btn-ghost" title="Edit"
                    aria-label={`Edit stack ${stack.name}`} disabled={busy || !canWrite}
                    onClick={() => setEditing(stack)}>
                    <PencilIcon />
                  </button>
                  <button className="btn btn-sm btn-icon btn-danger" title="Delete stack"
                    aria-label={`Delete stack ${stack.name}`} disabled={busy || !canWrite}
                    onClick={() => setPendingDelete(stack.name)}>
                    <TrashIcon />
                  </button>
                </header>
                <div className="module-card-body stack-card-body">
                  {stack.description && <p className="module-desc-full">{stack.description}</p>}

                  <Pipeline stack={stack} run={run} />

                  {members.length > 0 && (
                    <div className="stack-members">
                      <span className="faint">
                        {members.length} instance{members.length === 1 ? '' : 's'} · {up} running
                      </span>
                      <span className="template-members">
                        {members.map((m) => (
                          <button key={`${m.node}/${m.name}`} type="button"
                            className="template-member" title={`${m.status}${
                              m.template ? ` · from ${m.template}` : ''}`}
                            onClick={() => onOpen(m.name, m.node)}>
                            <span className={`dot${m.status === 'Running' ? ' dot-ok' : ''}`} />
                            {m.name}
                            {m.ipv4[0] && <span className="faint">{m.ipv4[0]}</span>}
                            {spread && <span className="badge badge-dim">{m.node}</span>}
                          </button>
                        ))}
                      </span>
                    </div>
                  )}

                  {missing.length > 0 && (
                    <p className="hint" style={{ color: 'var(--danger)' }}>
                      Launches {missing.map((s) => s.type === 'launch' && `“${s.template}”`)
                        .join(', ')}, which no longer exist. Edit the stack before launching it.
                    </p>
                  )}

                  <div className="stack-card-actions">
                    {running ? (
                      <button className="btn btn-danger btn-sm" disabled={!canWrite || run.cancelling}
                        onClick={() => cancel(stack.name)}
                        title="Start nothing new; launches under way finish">
                        <StopIcon size={13} /> {run.cancelling ? 'Cancelling…' : 'Cancel'}
                      </button>
                    ) : members.length === 0 ? (
                      <button className="btn btn-primary"
                        disabled={!ready || !canWrite || busy || missing.length > 0}
                        onClick={() => requestLaunch(stack)}>
                        {starting[stack.name] ? <span className="spinner" /> : <PlayIcon size={13} />}
                        Launch stack
                      </button>
                    ) : (
                      <>
                        <button className="btn btn-primary"
                          title="Destroy every instance of this stack and launch it again"
                          disabled={!ready || !canWrite || busy || missing.length > 0}
                          onClick={() => requestLaunch(stack, true)}>
                          {starting[stack.name] ? <span className="spinner" /> : <RestartIcon size={13} />}
                          Relaunch
                        </button>
                        {up > 0 ? (
                          <button className="btn btn-sm" disabled={!canWrite || busy}
                            onClick={() => setState(stack, 'stop')}>
                            {acting[stack.name] === 'stop' ? <span className="spinner" /> : <StopIcon size={13} />}
                            Stop all
                          </button>
                        ) : null}
                        {up < members.length ? (
                          <button className="btn btn-sm" disabled={!canWrite || busy}
                            onClick={() => setState(stack, 'start')}>
                            {acting[stack.name] === 'start' ? <span className="spinner" /> : <PlayIcon size={13} />}
                            Start all
                          </button>
                        ) : null}
                        <button className="btn btn-sm btn-danger" disabled={!canWrite || busy}
                          onClick={() => setDestroying(stack)}>
                          <TrashIcon size={13} /> Destroy
                        </button>
                      </>
                    )}
                    {running && <span className="hint">Running — started{' '}
                      {new Date(run.started_at * 1000).toLocaleTimeString()}</span>}
                    {run && !running && (
                      <>
                        <span className="hint">
                          Last {run.action === 'launch' ? 'run' : run.action}{' '}
                          {run.ok ? 'finished' : run.cancelled ? 'cancelled' : 'failed'}{' '}
                          {run.finished_at && new Date(run.finished_at * 1000).toLocaleTimeString()}
                        </span>
                        <button type="button" className="btn btn-ghost btn-icon btn-sm"
                          style={{ marginLeft: 'auto' }}
                          aria-label={`Dismiss the last run of ${stack.name}`} title="Dismiss"
                          onClick={() => dismiss(stack.name)}>
                          <CloseIcon size={13} />
                        </button>
                      </>
                    )}
                  </div>

                  {run && <RunDetail run={run} localNode={localNode} onOpen={onOpen} />}
                </div>
              </section>
            )
          })}
        </div>
      )}

      {editing && (
        <StackEditor
          stack={editing === 'new' ? undefined : editing}
          templates={templates}
          modules={modules}
          nodes={nodes}
          groups={groups}
          running={editing === 'new' ? 0 : membersOf(editing).length}
          onCancel={() => setEditing(null)}
          onSaved={(saved) => {
            onNotify(syncKind(saved.synced), `Saved stack “${saved.name}”`,
              syncDetail(saved.synced))
            setEditing(null)
            load()
          }}
        />
      )}

      {asking && (
        <LaunchDialog stack={asking.stack} asks={stackAsks(asking.stack, templates, modules)}
          // Live, so what the dialog lists is what gets destroyed.
          replace={asking.relaunch ? membersOf(asking.stack) : null}
          localNode={localNode}
          onCancel={() => setAsking(null)}
          onConfirm={(values, replace) => {
            const { stack } = asking
            setAsking(null)
            launch(stack, values, replace ?? undefined)
          }} />
      )}

      {destroying && (
        <ConfirmDialog
          title={`Destroy “${destroying.name}”?`}
          message="Every instance this stack launched is stopped and deleted, with its filesystem and snapshots. The stack and its templates are kept, so it can be launched again."
          confirmLabel={`Destroy ${membersOf(destroying).length}`}
          danger
          onConfirm={() => {
            const stack = destroying
            setDestroying(null)
            destroy(stack, membersOf(stack))
          }}
          onCancel={() => setDestroying(null)}
        >
          <MemberList members={membersOf(destroying)} localNode={localNode} />
        </ConfirmDialog>
      )}

      {pendingDelete && (
        <ConfirmDialog
          title={`Delete stack “${pendingDelete}”?`}
          message="This removes the saved sequence. Its templates, and any instances it launched, are untouched."
          confirmLabel="Delete"
          danger
          onConfirm={remove}
          onCancel={() => { setPendingDelete(null); setDeleteEverywhere(true) }}
        >
          {/* An instance keeps its tag when the definition goes, which is how
              stopping and destroying survive a restart -- so a later stack of
              the same name takes these up as its own. Said here, where the
              leftovers are made, rather than left to surprise somebody. */}
          {orphansOf(pendingDelete).length > 0 && (
            <>
              <div className="banner banner-warn"><div className="banner-body"><p>
                {orphansOf(pendingDelete).length} instance(s) stay tagged
                “{pendingDelete}” and would be taken up by a new stack of that
                name. Destroy the stack first to be rid of them.
              </p></div></div>
              <MemberList members={orphansOf(pendingDelete)} localNode={localNode} />
            </>
          )}
          {nodes.length > 1 && (
            <label className="check">
              <input type="checkbox" checked={deleteEverywhere}
                onChange={(event) => setDeleteEverywhere(event.target.checked)} />
              <span>
                Also remove it from the other {nodes.length - 1} node(s)
                <span className="dim"> — stacks are kept level across the cluster</span>
              </span>
            </label>
          )}
        </ConfirmDialog>
      )}
    </>
  )
}

function RunBadge({ run }: { run: StackRun }) {
  if (run.finished_at === null) {
    return <span className="badge badge-info"><span className="dot dot-pulse" />running</span>
  }
  if (run.ok) return <span className="badge badge-ok">done</span>
  return <span className="badge badge-danger">{run.cancelled ? 'cancelled' : 'failed'}</span>
}

function stepLabel(step: StackStep) {
  if (step.type === 'sleep') return `${step.seconds}s`
  if (step.type === 'wait_healthy') return 'healthy'
  return step.template
}

/** The stack left to right, one column per stage, coloured by the run if there is one. */
function Pipeline({ stack, run }: { stack: Stack; run?: StackRun }) {
  return (
    <div className="stack-pipeline" role="list" aria-label="Stages">
      {stack.stages.map((stage, index) => (
        <div key={index} className="stack-pipeline-stage" role="listitem">
          {stage.steps.map((step) => {
            // A run records the stack as it was launched; an edit since may
            // have changed which steps there are.
            const state = run?.stages.flatMap((s) => s.steps).find((s) => s.id === step.id)
            const icon = step.type === 'sleep' ? <ClockIcon size={12} />
              : step.type === 'wait_healthy' ? <HeartIcon size={12} /> : <ServerIcon size={12} />
            return (
              <div key={step.id} className={`stack-chip is-${state?.state ?? 'idle'}`}
                title={[step.id, state?.detail, state?.error].filter(Boolean).join(' — ')}>
                {icon}
                <span className="truncate">{stepLabel(step)}</span>
                {step.type === 'launch' && <span className="stack-chip-count">×{step.count}</span>}
                {state?.state === 'running' && state.until && (
                  <span className="stack-chip-count">{remaining(state.until)}</span>
                )}
              </div>
            )
          })}
        </div>
      ))}
    </div>
  )
}

const STATE_BADGE: Record<StackStepRun['state'], string> = {
  pending: 'badge-dim', running: 'badge-info', ready: 'badge-info', done: 'badge-ok',
  failed: 'badge-danger', skipped: 'badge-dim', cancelled: 'badge-dim',
}

/** Each step of a run: its state, and for a launch what it made and handed on. */
function RunDetail({ run, localNode, onOpen }: {
  run: StackRun
  localNode: string
  onOpen: (name: string, node?: string) => void
}) {
  const steps = run.stages.flatMap((stage, index) => stage.steps.map((step) => ({ step, index })))
  const spread = steps.some(({ step }) => (step.instances ?? []).some((i) => i.node !== localNode))
  return (
    <ul className="template-results stack-run">
      {steps.map(({ step, index }) => (
        <li key={step.id}>
          <span className="faint stack-run-stage">{index + 1}</span>
          <span className={`badge ${STATE_BADGE[step.state]}`}>
            {step.state === 'ready' ? 'bootstrapping' : step.state}
          </span>
          <span className="mono">
            {step.type === 'destroy' ? `destroy ${step.count} existing` : step.id}
          </span>
          {step.type === 'launch' && (step.instances ?? []).length > 0 && (
            <span className="template-members">
              {(step.instances ?? []).map((instance) => {
                // From the instance itself: `outputs.ips` leaves out whatever
                // had no address, so its positions are not the instances'.
                const ip = instance.ipv4
                return (
                  <button key={`${instance.node}/${instance.name}`} type="button"
                    className="template-member" title={instance.error ?? undefined}
                    onClick={() => onOpen(instance.name, instance.node)}>
                    <span className={`dot${instance.ok ? ' dot-ok' : ''}`}
                      style={instance.ok === false ? { color: 'var(--danger)' } : undefined} />
                    {instance.name}
                    {ip && <span className="faint">{ip}</span>}
                    {spread && <span className="badge badge-dim">{instance.node}</span>}
                  </button>
                )
              })}
            </span>
          )}
          {step.state === 'running' && step.detail && (
            <span className="faint">{step.detail}{step.until ? ` · ${remaining(step.until)}` : ''}</span>
          )}
          {step.error && <span className="stack-run-error">{step.error}</span>}
        </li>
      ))}
    </ul>
  )
}

function MemberList({ members, localNode }: { members: StackInstance[]; localNode: string }) {
  return (
    <ul className="template-results">
      {members.map((m) => (
        <li key={`${m.node}/${m.name}`}>
          <StatusBadge status={m.status} />
          <span className="mono">{m.name}</span>
          {m.node !== localNode && <span className="badge badge-dim">{m.node}</span>}
          {m.ipv4[0] && <span className="faint mono">{m.ipv4[0]}</span>}
        </li>
      ))}
    </ul>
  )
}

/**
 * Collects what a launch must be given -- secrets and `{{params.NAME}}`
 * inputs, which go to this run and nowhere else -- and, for a relaunch, lists
 * exactly which instances are destroyed first.
 */
function LaunchDialog({ stack, asks, replace, localNode, onCancel, onConfirm }: {
  stack: Stack
  asks: Ask[]
  replace: StackInstance[] | null
  localNode: string
  onCancel: () => void
  onConfirm: (values: Record<string, string>, replace: StackInstance[] | null) => void
}) {
  const [values, setValues] = useState<Record<string, string>>({})
  const complete = asks.every((p) => values[p.name])
  const relaunch = replace !== null
  return (
    <Modal
      title={relaunch ? `Relaunch “${stack.name}”?` : `Launch “${stack.name}”`}
      subtitle={relaunch
        ? 'These are stopped and deleted first, with everything inside them, then the stack runs again from the start.'
        : 'Secrets are never saved, so they are entered for each launch. Every step that needs one gets the same value.'}
      onClose={onCancel}
      footer={
        <>
          <button type="button" className="btn" onClick={onCancel}>Cancel</button>
          <button type="submit" form="stack-launch-form"
            className={`btn ${relaunch ? 'btn-danger' : 'btn-primary'}`}
            disabled={!complete}>
            {relaunch ? `Destroy ${replace.length} and relaunch` : 'Launch stack'}
          </button>
        </>
      }
    >
      <form id="stack-launch-form" style={{ display: 'contents' }}
        onSubmit={(event) => {
          event.preventDefault()
          if (complete) onConfirm(values, replace)
        }}>
        {relaunch && <MemberList members={replace} localNode={localNode} />}
        {asks.map((param) => (
          <div className="field" key={param.name}>
            <label htmlFor={`stack-ask-${param.name}`}>
              {param.name}
              {param.secret && <span className="badge badge-warn">secret</span>}
            </label>
            <input id={`stack-ask-${param.name}`} className="input mono"
              type={param.secret ? 'password' : 'text'}
              autoComplete={param.secret ? 'new-password' : 'off'} value={values[param.name] ?? ''}
              onChange={(event) => setValues((current) => ({
                ...current, [param.name]: event.target.value }))} />
            {param.description && <span className="hint">{param.description}</span>}
            <span className="hint faint">
              {param.goes.length > 0 ? (
                <>Fills {param.goes.map((where, i) => (
                  <span key={where}>{i > 0 && ', '}<span className="mono">{where}</span></span>
                ))}.</>
              ) : (
                <>Every module of every launch that declares <span className="mono">{param.name}</span>{' '}
                as a secret gets this value.</>
              )}
            </span>
          </div>
        ))}
      </form>
    </Modal>
  )
}
