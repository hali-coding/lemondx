import { Fragment, useCallback, useEffect, useState } from 'react'
import { useCanOperate, useCanWrite } from '../hooks/useAuth'
import { api, calls } from '../lib/api'
import type { SwarmSequenceStart } from '../lib/api'
import { syncDetail, syncKind } from '../lib/sync'
import type {
  BootstrapModule, ClusterNode, InstanceTemplate, NodeGroup, Stack, StackInstance,
  StackInstances, StackLaunchStep, StackRun, StackStep, StackStepRun, StackSwarm,
} from '../lib/types'
import { ConfirmDialog } from './ConfirmDialog'
import {
  ClockIcon, CloseIcon, DownloadIcon, GaugeIcon, HeartIcon, ListOrderIcon, PauseIcon, PencilIcon,
  PlayIcon, PlusIcon, RestartIcon, ServerIcon, StopIcon, TerminalIcon, TrashIcon,
} from './Icons'
import { Modal } from './Modal'
import { StackEditor } from './StackEditor'
import { StatusBadge } from './StatusBadge'
import { StaleBadge } from './StaleBadge'

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
    if (step.type !== 'launch' && step.type !== 'script') continue
    const values = Object.entries(step.params)
    if (step.type === 'script') values.push(['script', step.script])
    for (const [param, value] of values) {
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

/** The stack's secrets a launch has to be given: no value held, none to generate. */
function unsetSecrets(stack: Stack) {
  return Object.entries(stack.secrets).filter(([, s]) => !s.set && !s.generate)
    .map(([name, s]) => ({ name, description: s.description }))
}

/** Secrets any launch in the stack needs; asked for once, shared by all of them. */
function stackSecrets(stack: Stack, templates: InstanceTemplate[],
                      modules: BootstrapModule[]) {
  const found = new Map<string, Param>()
  for (const step of stack.stages.flatMap((s) => s.steps)) {
    if (step.type !== 'launch') continue
    const used = templates.find((t) => t.name === step.template)?.bootstrap.modules ?? []
    for (const module of modules) {
      if (!used.includes(module.id)) continue
      // One the step fills in itself is not asked for, and neither is one a
      // stack secret of the same name fills -- the server hands it over.
      for (const param of module.params) {
        if (param.secret && !(param.name in step.params) && !(param.name in stack.secrets)) {
          found.set(param.name, param)
        }
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
  const canOperate = useCanOperate()
  const [stacks, setStacks] = useState<Stack[] | null>(null)
  const [templates, setTemplates] = useState<InstanceTemplate[]>([])
  const [modules, setModules] = useState<BootstrapModule[]>([])
  const [nodes, setNodes] = useState<ClusterNode[]>([])
  const [groups, setGroups] = useState<NodeGroup[]>([])
  const [error, setError] = useState<string | null>(null)
  const [editing, setEditing] = useState<Stack | 'new' | null>(null)
  const [pendingDelete, setPendingDelete] = useState<string | null>(null)
  const [deleteEverywhere, setDeleteEverywhere] = useState(true)
  // Off unless ticked: deleting a design is not deciding to throw away what
  // it built, so the instances stay unless asked for.
  const [deleteInstances, setDeleteInstances] = useState(false)
  const [deleteBusy, setDeleteBusy] = useState(false)
  // `stale`: a relaunch asked for to replace stale instances, which says why.
  const [asking, setAsking] = useState<{ stack: Stack; relaunch: boolean; stale?: boolean } | null>(null)
  const [destroying, setDestroying] = useState<Stack | null>(null)
  // A swarm -- or, as a sequence, several -- about to start (its dialog), and
  // one whose leftovers are about to go.
  const [swarming, setSwarming] = useState<{ stack: Stack; swarm: StackSwarm | null } | null>(null)
  const [clearing, setClearing] = useState<{ stack: Stack; swarm: string } | null>(null)
  const [instances, setInstances] = useState<StackInstances | null>(null)
  const [acting, setActing] = useState<Record<string, string>>({})
  const [starting, setStarting] = useState<Record<string, boolean>>({})
  // Swarms whose sequence switch is being saved, by `stack/swarm`.
  const [toggling, setToggling] = useState<Record<string, boolean>>({})
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
                        replace?: StackInstance[], variables?: Record<string, string>,
                        secrets?: Record<string, string>) {
    setStarting((current) => ({ ...current, [stack.name]: true }))
    try {
      onRunStarted(await api.launchStack(stack.name, params, replace?.map(refOf), variables,
        secrets))
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

  function requestLaunch(stack: Stack, relaunch = false, stale = false) {
    // A relaunch always asks: it destroys what is there, which is worth a look.
    // So does a stack with variables (each launch may change them) or with a
    // secret that holds no value and makes none.
    if (relaunch || stackAsks(stack, templates, modules).length > 0
        || Object.keys(stack.variables).length > 0 || unsetSecrets(stack).length > 0) {
      setAsking({ stack, relaunch, stale })
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

  /** One swarm through its own route, or several in order as one run. */
  async function startSwarm(stack: Stack, sequence: boolean, body: SwarmSequenceStart) {
    const { swarms, counts, ...rest } = body
    setStarting((current) => ({ ...current, [stack.name]: true }))
    try {
      onRunStarted(sequence ? await api.startSwarmSequence(stack.name, body)
        : await api.startSwarm(stack.name, swarms[0], { ...rest, counts: counts?.[swarms[0]] }))
      window.setTimeout(() => loadInstances(), 1500)
    } catch (cause) {
      onNotify('error', `Could not start ${sequence ? 'swarms' : 'swarm'} “${swarms.join(' → ')}”`,
        (cause as Error).message)
    } finally {
      setStarting((current) => {
        const next = { ...current }
        delete next[stack.name]
        return next
      })
    }
  }

  /** Save a finished run's report, rendered by the server so the CLI's is the same. */
  async function exportReport(stack: Stack) {
    try {
      const report = await api.stackRunReport(stack.name)
      const url = URL.createObjectURL(new Blob([report.markdown], { type: 'text/markdown' }))
      const link = document.createElement('a')
      link.href = url
      link.download = report.filename
      link.click()
      // After the click has been handled: revoking at once can cancel it.
      window.setTimeout(() => URL.revokeObjectURL(url), 1000)
    } catch (cause) {
      onNotify('error', `Could not export the report of “${stack.name}”`, (cause as Error).message)
    }
  }

  /** Put a swarm in or out of the stack's sequence: a save, synced like one. */
  async function setSequence(stack: Stack, swarm: string, enabled: boolean) {
    const key = `${stack.name}/${swarm}`
    setToggling((current) => ({ ...current, [key]: true }))
    try {
      const saved = await api.setSwarmSequence(stack.name, swarm, enabled)
      onNotify(syncKind(saved.synced),
        enabled ? `“${swarm}” is back in the sequence` : `“${swarm}” is out of the sequence`,
        syncDetail(saved.synced) ?? (enabled ? undefined : 'It can still be run on its own.'))
      await load()
    } catch (cause) {
      onNotify('error', `Could not ${enabled ? 'enable' : 'disable'} “${swarm}”`,
        (cause as Error).message)
    } finally {
      setToggling((current) => {
        const next = { ...current }
        delete next[key]
        return next
      })
    }
  }

  /** Stop a running swarm (it tears itself down), or destroy what one left behind. */
  async function stopSwarm(stack: Stack, swarm: string, leftovers?: StackInstance[]) {
    try {
      const run = await api.stopSwarm(stack.name, swarm, leftovers?.map(refOf))
      if (leftovers) onRunStarted(run)
      else onNotify('info', `Stopping swarm “${swarm}”`,
        'Nothing new starts; then its instances are destroyed.')
      window.setTimeout(() => loadInstances(), 1500)
    } catch (cause) {
      onNotify('error', `Could not stop swarm “${swarm}”`, (cause as Error).message)
    }
    onChanged()
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

  function closeDelete() {
    setPendingDelete(null)
    setDeleteEverywhere(true)
    setDeleteInstances(false)
  }

  async function remove() {
    const name = pendingDelete
    if (!name) return
    // Exactly the list shown: the server refuses if it has changed since.
    const doomed = deleteInstances ? orphansOf(name).map(refOf) : undefined
    setDeleteBusy(true)
    try {
      const result = await api.deleteStack(name, deleteEverywhere, doomed)
      onNotify(syncKind(result.synced), result.destroyed?.length
        ? `Deleted stack “${name}” and destroyed ${result.destroyed.length} instance(s)`
        : `Deleted stack “${name}”`, syncDetail(result.synced))
      load()
      loadInstances()
    } catch (cause) {
      onNotify('error', 'Could not delete the stack', (cause as Error).message)
    } finally {
      setDeleteBusy(false)
      closeDelete()
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
            // The stack's own, apart from its swarms': a swarm is run against
            // these, and they decide whether the stack is launched at all.
            const ownMembers = members.filter((m) => !m.swarm)
            const up = members.filter((m) => m.status === 'Running').length
            const staleCount = members.filter((m) => m.stale?.length).length
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
                        {staleCount > 0 && (
                          <span className="stale-hint"> · {staleCount} stale — relaunch to update</span>
                        )}
                      </span>
                      <span className="template-members">
                        {members.map((m) => (
                          <button key={`${m.node}/${m.name}`} type="button"
                            className="template-member" title={`${m.status}${
                              m.template ? ` · from ${m.template}` : ''}`}
                            onClick={() => onOpen(m.name, m.node)}>
                            <span className={`dot${m.status === 'Running' ? ' dot-ok' : ''}`} />
                            {m.name}
                            <StaleBadge stale={m.stale ?? []} template={m.template}
                              stack={stack.name} />
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

                  {stack.swarms.length > 0 && (
                    <SwarmList stack={stack} run={run} members={members}
                      canStart={ready && canOperate && !busy && ownMembers.length > 0}
                      canOperate={canOperate}
                      onStart={(swarm) => setSwarming({ stack, swarm })}
                      onSequence={() => setSwarming({ stack, swarm: null })}
                      canEdit={canWrite}
                      toggling={(swarm) => !!toggling[`${stack.name}/${swarm}`]}
                      onToggle={(swarm, enabled) => setSequence(stack, swarm, enabled)}
                      onStop={(swarm) => stopSwarm(stack, swarm)}
                      onClear={(swarm) => setClearing({ stack, swarm })} />
                  )}

                  <div className="stack-card-actions">
                    {running ? (
                      <button className="btn btn-danger btn-sm" disabled={!canOperate || run.cancelling}
                        onClick={() => cancel(stack.name)}
                        title={run.action === 'swarm'
                          ? (run.swarms?.length ?? 0) > 1
                            ? 'Start nothing new, destroy the running swarm\'s instances, and skip the swarms after it'
                            : 'Start nothing new, then destroy the swarm\'s instances'
                          : 'Start nothing new; launches under way finish'}>
                        <StopIcon size={13} />{' '}
                        {run.cancelling ? (run.action === 'swarm' ? 'Stopping…' : 'Cancelling…')
                          : run.action === 'swarm'
                            ? (run.swarms?.length ?? 0) > 1 ? 'Stop swarms' : `Stop swarm ${run.swarm}`
                            : 'Cancel'}
                      </button>
                    ) : ownMembers.length === 0 && members.length === 0 ? (
                      <button className="btn btn-primary"
                        disabled={!ready || !canOperate || busy || missing.length > 0}
                        onClick={() => requestLaunch(stack)}>
                        {starting[stack.name] ? <span className="spinner" /> : <PlayIcon size={13} />}
                        Launch stack
                      </button>
                    ) : (
                      <>
                        <button className="btn btn-primary"
                          title="Destroy every instance of this stack and launch it again"
                          disabled={!ready || !canOperate || busy || missing.length > 0}
                          onClick={() => requestLaunch(stack, true)}>
                          {starting[stack.name] ? <span className="spinner" /> : <RestartIcon size={13} />}
                          Relaunch
                        </button>
                        {staleCount > 0 && (
                          <button className="btn btn-sm btn-warn"
                            title="Some instances were made from an older version of this stack or its templates"
                            disabled={!ready || !canOperate || busy || missing.length > 0}
                            onClick={() => requestLaunch(stack, true, true)}>
                            <RestartIcon size={13} /> Replace {staleCount} stale
                          </button>
                        )}
                        {up > 0 ? (
                          <button className="btn btn-sm" disabled={!canOperate || busy}
                            onClick={() => setState(stack, 'stop')}>
                            {acting[stack.name] === 'stop' ? <span className="spinner" /> : <StopIcon size={13} />}
                            Stop all
                          </button>
                        ) : null}
                        {up < members.length ? (
                          <button className="btn btn-sm" disabled={!canOperate || busy}
                            onClick={() => setState(stack, 'start')}>
                            {acting[stack.name] === 'start' ? <span className="spinner" /> : <PlayIcon size={13} />}
                            Start all
                          </button>
                        ) : null}
                        <button className="btn btn-sm btn-danger" disabled={!canOperate || busy}
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
                          Last {run.action === 'launch' ? 'run'
                            : run.action === 'swarm' ? ((run.swarms?.length ?? 0) > 1
                              ? `swarms ${run.swarms?.join(' → ')}` : `swarm ${run.swarm}`)
                              : run.action === 'swarm-stop' ? `clear-up of swarm ${run.swarm}`
                                : run.action}{' '}
                          {run.ok ? 'finished' : run.cancelled ? 'cancelled' : 'failed'}{' '}
                          {run.finished_at && new Date(run.finished_at * 1000).toLocaleTimeString()}
                        </span>
                        {run.action === 'swarm' && (
                          <button type="button" className="btn btn-sm" style={{ marginLeft: 'auto' }}
                            onClick={() => exportReport(stack)}
                            title="Download the swarm's outputs and results as a Markdown report">
                            <DownloadIcon size={13} /> Export report
                          </button>
                        )}
                        <button type="button" className="btn btn-ghost btn-icon btn-sm"
                          style={run.action === 'swarm' ? undefined : { marginLeft: 'auto' }}
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
          onSaved={(saved, closed) => {
            onNotify(syncKind(saved.synced), `Saved stack “${saved.name}”`,
              syncDetail(saved.synced))
            // Saved all the same: what it names may arrive by sync later.
            for (const warning of saved.warnings ?? []) onNotify('info', 'Worth checking', warning)
            // Saved from the side pane, the designer stays open on it.
            if (closed) setEditing(null)
            load()
          }}
        />
      )}

      {asking && (
        <LaunchDialog stack={asking.stack} asks={stackAsks(asking.stack, templates, modules)}
          // Live, so what the dialog lists is what gets destroyed.
          replace={asking.relaunch ? membersOf(asking.stack) : null}
          stale={asking.stale ? membersOf(asking.stack).filter((m) => m.stale?.length).length : 0}
          localNode={localNode}
          onCancel={() => setAsking(null)}
          onConfirm={(values, replace, variables, secrets) => {
            const { stack } = asking
            setAsking(null)
            launch(stack, values, replace ?? undefined, variables, secrets)
          }} />
      )}

      {swarming && (
        <SwarmDialog stack={swarming.stack} swarm={swarming.swarm} templates={templates}
          onCancel={() => setSwarming(null)}
          onConfirm={(body) => {
            const { stack, swarm } = swarming
            setSwarming(null)
            startSwarm(stack, swarm === null, body)
          }} />
      )}

      {clearing && (
        <ConfirmDialog
          title={`Destroy what swarm “${clearing.swarm}” left?`}
          message="Its run ended without destroying these (lemondx stopped mid-swarm, say). They are stopped and deleted; the stack's own instances stay."
          confirmLabel={`Destroy ${membersOf(clearing.stack).filter((m) => m.swarm === clearing.swarm).length}`}
          danger
          onConfirm={() => {
            const { stack, swarm } = clearing
            setClearing(null)
            stopSwarm(stack, swarm, membersOf(stack).filter((m) => m.swarm === swarm))
          }}
          onCancel={() => setClearing(null)}
        >
          <MemberList members={membersOf(clearing.stack).filter((m) => m.swarm === clearing.swarm)}
            localNode={localNode} />
        </ConfirmDialog>
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
          message={deleteInstances
            ? 'This stops and deletes the instances below, with their filesystems and snapshots, then removes the saved sequence. Its templates are untouched.'
            : 'This removes the saved sequence. Its templates, and any instances it launched, are untouched.'}
          confirmLabel={deleteInstances && orphansOf(pendingDelete).length
            ? `Destroy ${orphansOf(pendingDelete).length} and delete` : 'Delete'}
          danger
          busy={deleteBusy}
          onConfirm={remove}
          onCancel={closeDelete}
        >
          {/* An instance keeps its tag when the definition goes, which is how
              stopping and destroying survive a restart -- so a later stack of
              the same name takes these up as its own. Said here, where the
              leftovers are made, rather than left to surprise somebody. */}
          {orphansOf(pendingDelete).length > 0 && (
            <>
              <label className="check">
                <input type="checkbox" checked={deleteInstances} disabled={deleteBusy}
                  onChange={(event) => setDeleteInstances(event.target.checked)} />
                <span>
                  Also destroy its {orphansOf(pendingDelete).length} instance(s)
                  <span className="dim"> — stopped and deleted, with everything inside them</span>
                </span>
              </label>
              {!deleteInstances && (
                <div className="banner banner-warn"><div className="banner-body"><p>
                  {orphansOf(pendingDelete).length} instance(s) stay tagged
                  “{pendingDelete}” and would be taken up by a new stack of that
                  name.
                </p></div></div>
              )}
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

/** One line per launch step of a swarm: what it launches, and how many. */
function swarmSummary(swarm: StackSwarm) {
  return swarm.stages.map((stage) => stage.steps.map((step) => (
    step.type === 'launch' ? `${step.template} ×${step.count}`
      : step.type === 'sleep' ? `sleep ${step.seconds}s`
        : step.type === 'script' ? step.id : 'healthy'
  )).join(' | ')).join(' → ')
}

type SwarmStanding = 'queued' | 'running' | 'passed' | 'failed' | 'skipped' | 'stopped'

/** How one swarm of a sequence run stands, from its stages in the run. */
function sequenceState(run: StackRun, swarm: string): SwarmStanding | null {
  const steps = run.stages.filter((stage) => stage.swarm === swarm).flatMap((stage) => stage.steps)
  if (steps.length === 0) return null
  if (steps.some((step) => step.state === 'failed')) return 'failed'
  if (steps.every((step) => step.state === 'done')) return 'passed'
  if (steps.every((step) => step.state === 'skipped')) return 'skipped'
  if (steps.some((step) => step.state === 'cancelled')) return 'stopped'
  if (steps.every((step) => step.state === 'pending')) return 'queued'
  return 'running'
}

const STANDING_BADGE: Record<SwarmStanding, string> = {
  queued: 'badge-dim', running: 'badge-info', passed: 'badge-ok', failed: 'badge-danger',
  skipped: 'badge-dim', stopped: 'badge-dim',
}

/**
 * A stack's swarms: load to run against it while it runs. Each says what it
 * launches, can be started once the stack has instances, and is stopped --
 * which tears it down -- while it runs. With more than one, the stack's
 * sequence sits above them: the swarms switched into it, in order, each run
 * only once the one before it passed. What one left behind (lemondx stopped
 * mid-swarm) is listed with its own way out.
 */
function SwarmList({ stack, run, members, canStart, canOperate, canEdit, toggling, onStart,
  onSequence, onToggle, onStop, onClear }: {
  stack: Stack
  run?: StackRun
  members: StackInstance[]
  canStart: boolean
  canOperate: boolean
  /** Whether the caller may change the definition: the sequence switch is a save. */
  canEdit: boolean
  toggling: (swarm: string) => boolean
  onStart: (swarm: StackSwarm) => void
  onSequence: () => void
  onToggle: (swarm: string, enabled: boolean) => void
  onStop: (swarm: string) => void
  onClear: (swarm: string) => void
}) {
  const running = run && run.finished_at === null ? run : null
  // The latest run, if it was a sequence: what the strip colours, running or
  // finished, until the run is dismissed.
  const seqRun = run?.action === 'swarm' && (run.swarms?.length ?? 0) > 1 ? run : null
  // `!== false`: a record from a node older than sequences has no flag, and runs them all.
  const stored = stack.swarms.filter((w) => w.sequence !== false).map((w) => w.name)
  // A sequence running now runs what it was started with, whatever the
  // definition says since.
  const shown = seqRun && seqRun.finished_at === null ? seqRun.swarms ?? [] : stored
  const many = stack.swarms.length > 1
  return (
    <div className="stack-swarms">
      <span className="stack-swarms-label">
        <GaugeIcon size={13} /> Swarms
        <span className="faint"> — load run against this stack on demand, torn down when it ends</span>
      </span>
      {many && (
        <div className="stack-sequence-strip">
          <span className="stack-sequence-strip-label"><ListOrderIcon /> Sequence</span>
          {shown.length > 0 ? (
            <ol>
              {shown.map((name, index) => {
                const standing = seqRun ? sequenceState(seqRun, name) : null
                return (
                  <li key={name} className={standing ? `is-${standing}` : undefined}>
                    {index > 0 && <span className="stack-sequence-arrow" aria-hidden="true">→</span>}
                    <span className="stack-sequence-place">{index + 1}</span>
                    <span className="mono">{name}</span>
                    {standing && <span className={`badge ${STANDING_BADGE[standing]}`}>{standing}</span>}
                  </li>
                )
              })}
            </ol>
          ) : (
            <span className="faint">No swarm is in it; switch some on in the designer's Sequence tab.</span>
          )}
          {seqRun && running ? (
            <button type="button" className="btn btn-sm btn-danger" disabled={!canOperate || running.cancelling}
              onClick={() => onStop(running.swarm ?? shown[0])}
              title="Destroy the running swarm's instances and skip the swarms after it">
              <StopIcon size={13} /> {running.cancelling ? 'Stopping…' : 'Stop sequence'}
            </button>
          ) : (
            <button type="button" className="btn btn-sm btn-warn" onClick={onSequence}
              disabled={!canStart || shown.length === 0}
              title={!canStart ? 'Launch the stack first; a swarm runs against its instances'
                : 'Run these swarms in this order, each only if the one before it passed'}>
              <PlayIcon size={13} /> Run sequence
            </button>
          )}
        </div>
      )}
      {stack.swarms.map((swarm) => {
        const live = running?.action === 'swarm' && running.swarm === swarm.name
        const left = members.filter((m) => m.swarm === swarm.name)
        const place = stored.indexOf(swarm.name)
        return (
          <div key={swarm.name} className={`stack-swarm${live ? ' is-live' : ''}`}>
            {many && (
              <span className={`stack-sequence-place${place < 0 ? ' is-off' : ''}`}
                title={place < 0 ? 'Not in the sequence' : `Number ${place + 1} in the sequence`}>
                {place < 0 ? '–' : place + 1}
              </span>
            )}
            <div className="stack-swarm-text">
              <strong className="mono">{swarm.name}</strong>
              {swarm.description && <span className="faint">{swarm.description}</span>}
              <span className="faint mono truncate">{swarmSummary(swarm)}</span>
              {many && place < 0 && <span className="badge badge-dim">disabled · not in sequence</span>}
              {left.length > 0 && (
                <span className={`badge ${live ? 'badge-info' : 'badge-warn'}`}>
                  {left.length} instance{left.length === 1 ? '' : 's'}{live ? '' : ' left behind'}
                </span>
              )}
            </div>
            {live && !seqRun ? (
              <button type="button" className="btn btn-sm btn-danger"
                disabled={!canOperate || running?.cancelling} onClick={() => onStop(swarm.name)}>
                <StopIcon size={13} /> {running?.cancelling ? 'Stopping…' : 'Stop'}
              </button>
            ) : (
              <>
                {many && (
                  <button type="button" className="btn btn-sm"
                    disabled={!canEdit || toggling(swarm.name)}
                    onClick={() => onToggle(swarm.name, place < 0)}
                    title={place < 0 ? 'Put this swarm back in the stack\'s sequence'
                      : 'Leave this swarm out of the stack\'s sequence; it can still be run on its own'}>
                    {toggling(swarm.name) ? <span className="spinner" />
                      : place < 0 ? <PlayIcon size={13} /> : <PauseIcon size={13} />}
                    {place < 0 ? 'Enable' : 'Disable'}
                  </button>
                )}
                {left.length > 0 && !running && (
                  <button type="button" className="btn btn-sm btn-danger" disabled={!canOperate}
                    onClick={() => onClear(swarm.name)}
                    title="Destroy the instances this swarm left behind">
                    <TrashIcon size={13} /> Clear
                  </button>
                )}
                <button type="button" className="btn btn-sm btn-warn" disabled={!canStart}
                  onClick={() => onStart(swarm)}
                  title={canStart ? 'Run just this swarm against the stack'
                    : 'Launch the stack first; a swarm runs against its instances'}>
                  <GaugeIcon size={13} /> Run
                </button>
              </>
            )}
          </div>
        )
      })}
    </div>
  )
}

/**
 * Start a swarm: how many of each load generator this time (up to 200 per
 * step), the stack's variables for this run, any value its steps ask for at
 * start, and the notes that matter -- a template that is not ephemeral.
 * Opened with no `swarm`, it runs the stack's sequence, drawn as the timeline
 * it is; which swarms are in it, and their order, belong to the designer.
 */
function SwarmDialog({ stack, swarm, templates, onCancel, onConfirm }: {
  stack: Stack
  /** Null for the stack's sequence. */
  swarm: StackSwarm | null
  templates: InstanceTemplate[]
  onCancel: () => void
  onConfirm: (body: SwarmSequenceStart) => void
}) {
  const sequence = swarm === null
  const chosen = swarm ? [swarm] : stack.swarms.filter((w) => w.sequence !== false)
  const launchesOf = (w: StackSwarm) => w.stages.flatMap((s) => s.steps)
    .filter((s): s is StackLaunchStep => s.type === 'launch')
  const [counts, setCounts] = useState<Record<string, Record<string, number>>>(() =>
    Object.fromEntries(stack.swarms.map((w) => [w.name,
      Object.fromEntries(launchesOf(w).map((s) => [s.id, s.count]))])))
  const [variables, setVariables] = useState<Record<string, string>>(() =>
    Object.fromEntries(Object.entries(stack.variables).map(([n, v]) => [n, v.value])))
  const [params, setParams] = useState<Record<string, string>>({})
  const [secretValues, setSecretValues] = useState<Record<string, string>>({})
  const missingSecrets = unsetSecrets(stack)
  const inputs = [...new Set(chosen.flatMap((w) => w.inputs ?? []))]
  const launches = chosen.flatMap((w) => launchesOf(w).map((step) => ({ swarm: w.name, step })))
  const countOf = (w: string, step: StackLaunchStep) => counts[w]?.[step.id] ?? step.count
  const totalOf = (w: StackSwarm) => launchesOf(w).reduce((sum, s) => sum + (countOf(w.name, s) || 0), 0)
  const lasting = [...new Set(launches.filter(({ step }) =>
    templates.find((t) => t.name === step.template)?.ephemeral === false)
    .map(({ step }) => step.template))]
  const largest = Math.max(0, ...chosen.map(totalOf))
  const changedCounts: Record<string, Record<string, number>> = {}
  for (const { swarm: w, step } of launches) {
    if (countOf(w, step) !== step.count) {
      changedCounts[w] = { ...changedCounts[w], [step.id]: countOf(w, step) }
    }
  }
  const overrides = Object.fromEntries(Object.entries(variables)
    .filter(([n, v]) => v !== stack.variables[n]?.value))
  const complete = chosen.length > 0 && inputs.every((n) => params[n])
    && missingSecrets.every((s) => secretValues[s.name])
    && launches.every(({ swarm: w, step }) => countOf(w, step) >= 1 && countOf(w, step) <= 200)
  const body: SwarmSequenceStart = { swarms: chosen.map((w) => w.name), counts: changedCounts,
    variables: overrides, params, secrets: secretValues }
  const call = sequence ? calls.startSwarmSequence(stack.name, body)
    : calls.startSwarm(stack.name, swarm.name, { counts: changedCounts[swarm.name] ?? {},
      variables: overrides, params, secrets: secretValues })

  const countField = (w: string, step: StackLaunchStep) => (
    <div className="field" key={`count-${w}-${step.id}`}>
      <label htmlFor={`swarm-count-${w}-${step.id}`}>
        {step.id} <span className="faint">· {step.template}</span>
      </label>
      <input id={`swarm-count-${w}-${step.id}`} className="input num" type="number" min={1}
        max={200} value={countOf(w, step)}
        onChange={(event) => setCounts((current) => ({ ...current, [w]: { ...current[w],
          [step.id]: Math.min(200, Math.max(0, Math.floor(Number(event.target.value) || 0))) } }))} />
      <span className="hint faint">
        Instances, this run only (designed: {step.count}, up to 200).
      </span>
    </div>
  )

  return (
    <Modal
      title={sequence ? `Run the sequence of “${stack.name}”`
        : `Swarm “${swarm.name}” against “${stack.name}”`}
      subtitle={sequence
        ? `${chosen.length} swarm${chosen.length === 1 ? '' : 's'}, one after another, against the stack's running instances — at most ${largest} instance${largest === 1 ? '' : 's'} at a time.`
        : `Launches ${largest} instance${largest === 1 ? '' : 's'} and runs the swarm's steps against the stack's running instances. However it ends — done, failed or stopped — its instances are destroyed.`}
      onClose={onCancel}
      api={{ ...call,
        secrets: [...inputs.map((n) => `params.${n}`), ...missingSecrets.map((s) => `secrets.${s.name}`)] }}
      footer={
        <>
          <button type="button" className="btn" onClick={onCancel}>Cancel</button>
          <button type="submit" form="swarm-start-form" className="btn btn-warn"
            disabled={!complete}>
            {sequence ? <><PlayIcon size={13} /> Run sequence</> : <><GaugeIcon size={13} /> Start swarm</>}
          </button>
        </>
      }
    >
      <form id="swarm-start-form" style={{ display: 'contents' }}
        onSubmit={(event) => {
          event.preventDefault()
          if (complete) onConfirm(body)
        }}>
        {sequence && (
          <ol className="swarm-timeline">
            {chosen.map((w, index) => (
              <li key={w.name}>
                <div className="swarm-timeline-head">
                  <span className="stack-sequence-place">{index + 1}</span>
                  <strong className="mono">{w.name}</strong>
                  <span className="faint">
                    {index === 0 ? 'runs first' : `runs if ${chosen[index - 1].name} passed`}
                    {' · '}{totalOf(w)} instance{totalOf(w) === 1 ? '' : 's'}
                  </span>
                </div>
                {w.description && <p className="faint swarm-timeline-about">{w.description}</p>}
                <div className="swarm-timeline-body">
                  {launchesOf(w).map((step) => countField(w.name, step))}
                </div>
                <p className="swarm-timeline-after">
                  {index < chosen.length - 1
                    ? `Its instances are destroyed. If it failed or was stopped, nothing after it runs.`
                    : 'Its instances are destroyed, and the sequence is done.'}
                </p>
              </li>
            ))}
          </ol>
        )}
        {sequence && stack.swarms.length > chosen.length && (
          <p className="hint faint" style={{ margin: 0 }}>
            Not in the sequence: {stack.swarms.filter((w) => w.sequence === false).map((w) => w.name).join(', ')}.
            Which swarms run, and in what order, is set in the stack's designer.
          </p>
        )}
        {lasting.length > 0 && (
          <div className="banner banner-warn"><div className="banner-body"><p>
            {lasting.join(', ')} {lasting.length === 1 ? 'is' : 'are'} not ephemeral. The swarm
            still destroys its instances when it ends, but if lemondx stops mid-swarm they stay
            until the swarm is stopped from this tab.
          </p></div></div>
        )}
        {!sequence && launches.map(({ swarm: w, step }) => countField(w, step))}
        {Object.entries(stack.variables).map(([name, variable]) => (
          <div className="field" key={`var-${name}`}>
            <label htmlFor={`swarm-var-${name}`}>
              {name} <span className="badge badge-ok">variable</span>
            </label>
            <input id={`swarm-var-${name}`} className="input mono" value={variables[name] ?? ''}
              onChange={(event) => setVariables((current) => ({
                ...current, [name]: event.target.value }))} />
            {variable.description && <span className="hint">{variable.description}</span>}
          </div>
        ))}
        {inputs.map((name) => (
          <div className="field" key={`in-${name}`}>
            <label htmlFor={`swarm-in-${name}`}>{name}</label>
            <input id={`swarm-in-${name}`} className="input mono" value={params[name] ?? ''}
              type={/pass|secret|token|key/i.test(name) ? 'password' : 'text'}
              onChange={(event) => setParams((current) => ({
                ...current, [name]: event.target.value }))} />
            <span className="hint faint">Asked for by the swarm's steps as {`{{params.${name}}}`}.</span>
          </div>
        ))}
        {missingSecrets.map((secret) => (
          <div className="field" key={`secret-${secret.name}`}>
            <label htmlFor={`swarm-secret-${secret.name}`}>
              {secret.name} <span className="badge badge-danger">stack secret</span>
            </label>
            <input id={`swarm-secret-${secret.name}`} className="input mono" type="password"
              autoComplete="new-password" value={secretValues[secret.name] ?? ''}
              onChange={(event) => setSecretValues((current) => ({
                ...current, [secret.name]: event.target.value }))} />
            <span className="hint faint">Kept with the stack after this, so it is only asked once.</span>
          </div>
        ))}
      </form>
    </Modal>
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
  if (step.type === 'script') return step.id
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
              : step.type === 'wait_healthy' ? <HeartIcon size={12} />
                : step.type === 'script' ? <TerminalIcon size={12} /> : <ServerIcon size={12} />
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
  // A sequence's steps under the swarm each belongs to; two swarms may share
  // a step id, and each has its own teardown.
  const sequence = run.action === 'swarm' && (run.swarms?.length ?? 0) > 1
  const opens = (index: number) => sequence && run.stages[index].swarm
    && (index === 0 || run.stages[index - 1].swarm !== run.stages[index].swarm)
  return (
    <ul className="template-results stack-run">
      {steps.map(({ step, index }, at) => (
        <Fragment key={`${index}-${step.id}`}>
        {opens(index) && at === steps.findIndex((s) => s.index === index) && (
          <li className="stack-run-swarm">
            <GaugeIcon size={12} />
            <strong className="mono">{run.stages[index].swarm}</strong>
            <span className="faint">
              swarm {(run.swarms ?? []).indexOf(run.stages[index].swarm ?? '') + 1} of {run.swarms?.length}
            </span>
          </li>
        )}
        <li>
          <span className="faint stack-run-stage">{index + 1}</span>
          <span className={`badge ${STATE_BADGE[step.state]}`}>
            {step.state === 'ready' ? 'bootstrapping' : step.state}
          </span>
          <span className="mono">
            {step.type === 'destroy'
              ? `destroy ${step.count} ${step.swarm ? 'swarm instance(s)' : 'existing'}`
              : step.id}
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
          {(step.type === 'script' || step.type === 'launch') && step.state === 'done'
            && step.detail && <span className="faint">{step.detail}</span>}
          {/* Why a swarm step stopped early: another step failed. */}
          {step.state === 'cancelled' && step.detail && <span className="faint">{step.detail}</span>}
          {step.error && <span className="stack-run-error">{step.error}</span>}
          {step.type === 'script' && step.output && (
            <details className="stack-run-output" open={step.state === 'failed'}>
              <summary>output</summary>
              <pre className="mono">{step.output}</pre>
            </details>
          )}
          {step.type === 'launch' && (step.instances ?? []).some((i) => i.output) && (
            // What each instance's modules said last -- a swarm's load
            // generators report their result here, before they are destroyed.
            <details className="stack-run-output" open={run.action === 'swarm'}>
              <summary>{run.action === 'swarm' ? 'results' : 'module output'}</summary>
              <pre className="mono">{(step.instances ?? []).filter((i) => i.output)
                .map((i) => `${i.name}: ${i.output}`).join('\n')}</pre>
            </details>
          )}
        </li>
        </Fragment>
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
function LaunchDialog({ stack, asks, replace, stale = 0, localNode, onCancel, onConfirm }: {
  stack: Stack
  asks: Ask[]
  replace: StackInstance[] | null
  /** How many are stale, when the relaunch was asked for to replace them. */
  stale?: number
  localNode: string
  onCancel: () => void
  onConfirm: (values: Record<string, string>, replace: StackInstance[] | null,
              variables: Record<string, string>, secrets: Record<string, string>) => void
}) {
  const [values, setValues] = useState<Record<string, string>>({})
  // The stack's variables as saved; what is changed here is for this launch only.
  const [variables, setVariables] = useState<Record<string, string>>(() =>
    Object.fromEntries(Object.entries(stack.variables).map(([n, v]) => [n, v.value])))
  const [secretValues, setSecretValues] = useState<Record<string, string>>({})
  const missingSecrets = unsetSecrets(stack)
  const overrides = Object.fromEntries(Object.entries(variables)
    .filter(([n, v]) => v !== stack.variables[n]?.value))
  const complete = asks.every((p) => values[p.name])
    && missingSecrets.every((s) => secretValues[s.name])
  const relaunch = replace !== null
  return (
    <Modal
      title={stale ? `Replace stale instances of “${stack.name}”?`
        : relaunch ? `Relaunch “${stack.name}”?` : `Launch “${stack.name}”`}
      subtitle={stale && relaunch
        ? `${stale} of ${replace.length} are stale. A stack is replaced as a whole, because its steps hand addresses and values to each other — so all ${replace.length} are stopped and deleted, with everything inside them, and the stack runs again from the start.`
        : relaunch
          ? 'These are stopped and deleted first, with everything inside them, then the stack runs again from the start.'
          : asks.length
            ? 'Values entered here go to this launch only. Every step that needs one gets the same value.'
            : 'Check the stack’s variables; a change applies to this launch only.'}
      onClose={onCancel}
      api={{
        ...calls.launchStack(stack.name, values, replace?.map(refOf), overrides, secretValues),
        secrets: [...asks.filter((p) => p.secret).map((p) => `params.${p.name}`),
          ...missingSecrets.map((s) => `secrets.${s.name}`)],
      }}
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
          if (complete) onConfirm(values, replace, overrides, secretValues)
        }}>
        {relaunch && <MemberList members={replace} localNode={localNode} />}
        {Object.entries(stack.variables).map(([name, variable]) => (
          <div className="field" key={`var-${name}`}>
            <label htmlFor={`stack-var-${name}`}>
              {name} <span className="badge badge-ok">variable</span>
              {variables[name] !== variable.value && (
                <span className="badge badge-dim">changed for this launch</span>
              )}
            </label>
            <input id={`stack-var-${name}`} className="input mono" value={variables[name] ?? ''}
              onChange={(event) => setVariables((current) => ({
                ...current, [name]: event.target.value }))} />
            {variable.description && <span className="hint">{variable.description}</span>}
          </div>
        ))}
        {Object.entries(stack.secrets).map(([name, secret]) => {
          const missing = !secret.set && !secret.generate
          return (
            <div className="field" key={`secret-${name}`}>
              <label htmlFor={`stack-secret-${name}`}>
                {name} <span className="badge badge-danger">stack secret</span>
              </label>
              {missing ? (
                <input id={`stack-secret-${name}`} className="input mono" type="password"
                  autoComplete="new-password" value={secretValues[name] ?? ''}
                  onChange={(event) => setSecretValues((current) => ({
                    ...current, [name]: event.target.value }))} />
              ) : (
                <span className="hint">
                  {secret.set ? 'Stored on the stack; used as it is.'
                    : 'No value yet: one is generated now and kept for later launches.'}
                </span>
              )}
              {secret.description && <span className="hint">{secret.description}</span>}
              {missing && (
                <span className="hint faint">
                  Kept with the stack after this launch, so it is only asked for once.
                </span>
              )}
            </div>
          )
        })}
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
