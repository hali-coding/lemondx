import { useCallback, useEffect, useRef, useState } from 'react'
import { ApiError, api, hasToken, setToken } from './lib/api'
import type {
  AuthInfo, CreateProgress, CreateRequest, HealthRecord, HealthStatus, InstanceRef,
  ScopedContainer, StateAction, Status, TemplateRun,
} from './lib/types'
import { keyOf } from './lib/instance'
import { AuthContext } from './hooks/useAuth'
import { useScope, scopeTargets } from './hooks/useScope'
import { useTheme } from './hooks/useTheme'
import { useToasts } from './hooks/useToasts'
import type { ToastKind } from './hooks/useToasts'
import { AccessView } from './components/AccessView'
import { ConfirmDialog } from './components/ConfirmDialog'
import { ContainerDrawer } from './components/ContainerDrawer'
import { ContainerTable } from './components/ContainerTable'
import { CreateDialog } from './components/CreateDialog'
import { MoonIcon, PlusIcon, RefreshIcon, SunIcon } from './components/Icons'
import { ModulesView } from './components/ModulesView'
import { NetworkView } from './components/NetworkView'
import { NodesView } from './components/NodesView'
import { ResourcesView } from './components/ResourcesView'
import { ScopePicker } from './components/ScopePicker'
import { SetupBanner } from './components/SetupBanner'
import { StorageView } from './components/StorageView'
import { LoginGate } from './components/LoginGate'
import { TemplatesView } from './components/TemplatesView'
import { Toasts } from './components/Toasts'

const POLL_INTERVAL = 3000

const VIEWS = ['containers', 'templates', 'nodes', 'resources', 'storage', 'network', 'modules', 'access'] as const

const STATE_VERB: Record<StateAction, string> = {
  start: 'Started', stop: 'Stopped', restart: 'Restarted',
  freeze: 'Paused', unfreeze: 'Resumed',
}

export default function App() {
  const { theme, toggle } = useTheme()
  const { scope, choose: chooseScope, nodes, groups, federated, reload: reloadCluster } = useScope()
  // Where this lemondx sits, so a row here can be told apart from one elsewhere.
  const localNode = nodes.find((n) => n.self)?.name ?? ''
  const nodeUrls = Object.fromEntries(nodes.map((n) => [n.name, n.url]))
  const { toasts, push, dismiss } = useToasts()

  const [status, setStatus] = useState<Status | null>(null)
  const [containers, setContainers] = useState<ScopedContainer[] | null>(null)
  // Nodes that could not be reached for the current scope, so the table can say
  // it is showing less than was asked for rather than quietly showing less.
  const [scopeErrors, setScopeErrors] = useState<{ node: string; error: string }[]>([])
  const [creates, setCreates] = useState<CreateProgress[]>([])
  const [templateRuns, setTemplateRuns] = useState<TemplateRun[]>([])
  const [health, setHealth] = useState<Record<string, HealthRecord>>({})
  const [connectionError, setConnectionError] = useState<string | null>(null)
  const [busy, setBusy] = useState<Record<string, boolean>>({})
  // The whole record, not a name: the drawer needs the name, the busy map and
  // the table need the node-qualified key, and in a cluster those differ.
  const [selected, setSelected] = useState<ScopedContainer | null>(null)
  const [showCreate, setShowCreate] = useState(false)
  const [pendingDelete, setPendingDelete] = useState<ScopedContainer | null>(null)
  const [deleting, setDeleting] = useState(false)
  const [refreshToken, setRefreshToken] = useState(0)
  const [authInfo, setAuthInfo] = useState<AuthInfo | null>(null)
  // Set while the server refuses us; holds what it accepts instead.
  const [gate, setGate] = useState<AuthInfo | null>(null)
  const [view, setView] = useState<(typeof VIEWS)[number]>('containers')

  // Any in-flight mutation pauses polling so it cannot clobber optimistic state.
  // That stops new polls only; refreshSequence drops a response once a newer
  // refresh has started, so one already under way cannot land after the
  // refresh following a mutation -- nor re-watch a job that refresh reported
  // finished, which would report it a second time.
  const mutating = useRef(0)
  const refreshSequence = useRef(0)
  // Bumped whenever a credential is presented, so a 401 from a request that was
  // already in flight when the user signed in cannot re-open the gate they have
  // just closed -- nor, in token mode, throw away the token they just pasted.
  const authEpoch = useRef(0)
  // Creates and template runs are started on the server and return at once,
  // so a poll is the only way a page hears how one ended -- whether this page
  // started it, another tab did, or it began before a reload. These hold the
  // ones a poll (or the request starting them) has seen still running; a later
  // poll finding one finished reports it, exactly once.
  const watchedCreates = useRef(new Set<string>())
  const watchedRuns = useRef(new Set<string>())
  // The health status each instance had at this page's previous poll, so a
  // change is reported once, by the page that saw it happen -- and a page
  // opened on an instance that is already unhealthy says nothing.
  const seenHealth = useRef<Map<string, HealthStatus> | null>(null)

  const notify = useCallback(
    (kind: ToastKind, title: string, detail?: string) => push(kind, title, detail),
    [push],
  )

  const reportCreates = useCallback((records: CreateProgress[]) => {
    for (const record of records) {
      if (record.finished_at === null) {
        watchedCreates.current.add(record.name)
        continue
      }
      if (!watchedCreates.current.delete(record.name)) continue
      // Template runs report as a whole, below.
      if (record.template) continue
      if (record.error) {
        notify('error', `Could not create ${record.name}`, record.error)
      } else if (record.ok === false) {
        notify('error', `Bootstrap of ${record.name} failed on “${record.failed_module ?? 'a module'}”`,
          record.error_detail ?? 'The container was created, but a module did not finish.')
      } else {
        notify('success', `Created ${record.name}`, record.modules
          ? `${record.modules} module(s) ran successfully.` : undefined)
      }
    }
  }, [notify])

  const reportRuns = useCallback((runs: TemplateRun[]) => {
    for (const run of runs) {
      const key = `${run.template}@${run.started_at}`
      if (run.finished_at === null) {
        watchedRuns.current.add(key)
        continue
      }
      if (!watchedRuns.current.delete(key)) continue
      const instances = run.result?.instances ?? []
      const failed = instances.filter((i) => !i.ok)
      if (run.action === 'exec') {
        const what = `\`${run.command}\``
        if (run.error) {
          notify('error', `Could not run ${what} on “${run.template}”`, run.error)
        } else if (failed.length > 0) {
          notify('error', `${what} failed on ${failed.length} of ${instances.length} from “${run.template}”`,
            failed.map((i) => `${i.name}: ${i.error ?? `exit ${i.exec?.exit_code}`}`).join('; '))
        } else {
          notify('success', `Ran ${what} on ${instances.length} from “${run.template}”`,
            'Output is on the template’s card.')
        }
        continue
      }
      const done = { launch: 'Launched', recreate: 'Recreated', destroy: 'Destroyed' }[run.action]
      if (run.error) {
        notify('error', `Could not ${run.action} from “${run.template}”`, run.error)
      } else if (failed.length > 0) {
        notify('error', `${failed.length} of ${instances.length} from “${run.template}” failed`,
          failed.map((i) => `${i.name}: ${i.error ?? 'a module failed'}`).join('; '))
      } else {
        notify('success', `${done} ${instances.length} from “${run.template}”`,
          instances.map((i) => i.name).join(', '))
      }
    }
  }, [notify])

  const reportHealth = useCallback((records: HealthRecord[]) => {
    const previous = seenHealth.current
    const next = new Map(records.map((r) => [r.name, r.status] as [string, HealthStatus]))
    seenHealth.current = next
    if (previous === null) return
    for (const record of records) {
      const before = previous.get(record.name)
      if (before === undefined || before === record.status) continue
      if (record.status === 'unhealthy') {
        notify('error', `${record.name} is unhealthy`, record.reasons.join('; ') || undefined)
      } else if (before === 'unhealthy' && record.status === 'healthy') {
        notify('success', `${record.name} recovered`)
      }
    }
  }, [notify])

  const refresh = useCallback(async (signal?: AbortSignal) => {
    const sequence = ++refreshSequence.current
    const epoch = authEpoch.current
    try {
      const [nextStatus, nextContainers, nextCreates, nextRuns, nextHealth] = await Promise.all([
        api.status(signal),
        // One fetch drives both the Containers and Templates tabs, so they can
        // never disagree about which hosts are in view.
        scope.kind === 'local'
          ? api.listContainers(signal).then((list) => ({ instances: list, errors: [] }))
          : api.clusterContainers(scopeTargets(scope), signal),
        // Progress is extra; a failure here must not look like losing the server.
        api.creates(signal).catch(() => null),
        api.templateRuns(signal).catch(() => null),
        // Read from the server's memory, so polling it this often is free; the
        // checks themselves run on the server's own interval.
        api.health(signal).catch(() => null),
      ])
      if (sequence !== refreshSequence.current) return
      setStatus(nextStatus)
      setContainers(nextContainers.instances)
      setScopeErrors(nextContainers.errors)
      if (nextCreates) {
        setCreates(nextCreates)
        reportCreates(nextCreates)
      }
      if (nextRuns) {
        setTemplateRuns(nextRuns)
        reportRuns(nextRuns)
      }
      if (nextHealth) {
        setHealth(Object.fromEntries(nextHealth.instances.map((r) => [r.name, r])))
        reportHealth(nextHealth.instances)
      }
      setConnectionError(null)
    } catch (cause) {
      if ((cause as Error).name === 'AbortError' || sequence !== refreshSequence.current) return
      if (cause instanceof ApiError && cause.status === 401) {
        // Signed in since this request went out: it was refused for want of a
        // credential that now exists, and acting on that would undo the login.
        if (epoch !== authEpoch.current) return
        // A pasted token that stopped working (revoked, expired, mistyped)
        // would otherwise be sent forever, shadowing a fresh login.
        if (hasToken()) setToken(null)
        api.authInfo().then((info) => {
          setAuthInfo(info)
          setGate(info)
        }).catch(() => {})
        return
      }
      setConnectionError((cause as Error).message)
    }
  }, [reportCreates, reportRuns, reportHealth, scope])

  useEffect(() => {
    const controller = new AbortController()
    api.authInfo(controller.signal).then(setAuthInfo).catch(() => {})
    return () => controller.abort()
  }, [])

  const signedIn = useCallback(async (info: AuthInfo | null) => {
    authEpoch.current += 1
    // Closing the gate restarts the poll, which refreshes everything; calling
    // refresh() here as well would only duplicate that first round.
    setGate(null)
    // The cluster was asked about before the login and refused, leaving this
    // node nameless and the scope picker missing until a reload.
    reloadCluster()
    // A pasted token passes null: ask who it makes us.
    setAuthInfo(info ?? await api.authInfo().catch(() => null))
  }, [reloadCluster])

  const logout = useCallback(async () => {
    setToken(null)
    await api.logout().catch(() => {})
    const info = await api.authInfo().catch(() => null)
    setAuthInfo(info)
    if (info?.enabled && !info.principal) setGate(info)
  }, [])

  useEffect(() => {
    // Nothing to poll for while the login gate is up: every request is refused,
    // and every refusal asks the server again what it accepts.
    if (gate) return
    const controller = new AbortController()
    // oxlint-disable-next-line react/set-state-in-effect -- async fetch, not a sync setState
    refresh(controller.signal)
    const timer = window.setInterval(() => {
      if (mutating.current === 0) refresh()
    }, POLL_INTERVAL)
    return () => {
      controller.abort()
      window.clearInterval(timer)
    }
  }, [refresh, gate])

  /** Run a mutation with busy tracking, toasts and a refresh afterwards. */
  const mutate = useCallback(async (
    name: string,
    run: () => Promise<void>,
    onSuccess?: () => void,
    onFailure?: (message: string) => void,
  ) => {
    mutating.current += 1
    setBusy((current) => ({ ...current, [name]: true }))
    try {
      await run()
      onSuccess?.()
    } catch (cause) {
      const message = cause instanceof ApiError
        ? cause.message
        : (cause as Error).message
      onFailure?.(message)
    } finally {
      mutating.current -= 1
      setBusy((current) => {
        const next = { ...current }
        delete next[name]
        return next
      })
      await refresh()
      setRefreshToken((token) => token + 1)
    }
  }, [refresh])

  const changeState = useCallback((container: ScopedContainer, action: StateAction) => {
    const { name, node } = container
    const where = node && node !== localNode ? ` on ${node}` : ''
    return mutate(
      keyOf(container),
      // A row on this node takes the plain call it always did; only one
      // elsewhere goes through the cluster, which needs the node to route it.
      () => (node && node !== localNode
        ? api.setStateAcross([{ node, name }], action).then(() => {})
        : api.setState(name, action).then(() => {})),
      () => notify('success', `${STATE_VERB[action]} ${name}${where}`),
      (message) => notify('error', `Could not ${action} ${name}${where}`, message),
    )
  }, [mutate, notify, localNode])

  /**
   * One action over the containers the user ticked and confirmed. The server
   * applies it to each and reports every outcome, so a container that refuses
   * does not hide what happened to the rest; resolves false only when the
   * request itself never landed, which leaves the selection to try again.
   */
  const bulkState = useCallback(async (instances: InstanceRef[], action: StateAction) => {
    const keys = instances.map((i) => (i.node === localNode ? i.name : `${i.node}/${i.name}`))
    const names = instances.map((i) => i.name)
    const spread = instances.some((i) => i.node !== localNode)
    mutating.current += 1
    setBusy((current) => ({ ...current, ...Object.fromEntries(keys.map((k) => [k, true])) }))
    try {
      const result = spread
        ? await api.setStateAcross(instances, action)
        : await api.setStateMany(names, action)
      const failed = result.instances.filter((i) => !i.ok)
      if (failed.length === 0) {
        notify('success', `${STATE_VERB[action]} ${names.length} container${names.length === 1 ? '' : 's'}`,
          names.join(', '))
      } else {
        notify('error', `${failed.length} of ${result.instances.length} could not ${action}`,
          failed.map((i) => `${i.name}: ${i.error}`).join('; '))
      }
      return true
    } catch (cause) {
      notify('error', `Could not ${action} the selected containers`,
        cause instanceof ApiError ? cause.message : (cause as Error).message)
      return false
    } finally {
      mutating.current -= 1
      setBusy((current) => {
        const next = { ...current }
        for (const key of keys) delete next[key]
        return next
      })
      await refresh()
      setRefreshToken((token) => token + 1)
    }
  }, [notify, refresh, localNode])

  const create = useCallback(async (request: CreateRequest) => {
    // Returns once the server has accepted the create -- anything it can
    // refuse up front, it refuses here, for the dialog to show. The create
    // itself carries on on the server whatever this page does next.
    const started = await api.createContainer(request)
    watchedCreates.current.add(started.name)
    setCreates((current) => [...current.filter((c) => c.name !== started.name), started])
    notify('info', `Creating ${started.name}`, started.modules
      ? `Then running ${started.modules} module(s). Progress shows in the list.`
      : 'Progress shows in the list.')
    refresh()
  }, [notify, refresh])

  const templateRunStarted = useCallback((run: TemplateRun) => {
    watchedRuns.current.add(`${run.template}@${run.started_at}`)
    setTemplateRuns((current) => [...current.filter((r) => r.template !== run.template), run])
    // New or disappearing rows should show up before the next regular poll.
    window.setTimeout(() => refresh(), 800)
  }, [refresh])

  const confirmDelete = useCallback(async () => {
    const target = pendingDelete
    if (!target) return
    const { name, node } = target
    const key = keyOf(target)
    const where = node && node !== localNode ? ` on ${node}` : ''
    setDeleting(true)
    // force also stops a running container first.
    await mutate(
      key,
      () => (node && node !== localNode
        ? api.deleteAcross([{ node, name }], true).then(() => {})
        : api.deleteContainer(name, true).then(() => {})),
      () => {
        notify('success', `Deleted ${name}${where}`)
        setSelected((current) => (current && keyOf(current) === key ? null : current))
      },
      (message) => notify('error', `Could not delete ${name}${where}`, message),
    )
    setDeleting(false)
    setPendingDelete(null)
  }, [mutate, notify, pendingDelete, localNode])

  /** Open an instance's drawer by name, for the Templates tab's member lists. */
  const openInstance = useCallback((name: string, node?: string) => {
    const found = (containers ?? []).find(
      (c) => c.name === name && (node === undefined || (c.node ?? localNode) === node))
    if (found) setSelected(found)
  }, [containers, localNode])

  const runSetup = useCallback(async (storageDriver: string) => {
    try {
      const result = await api.setup({ storage_driver: storageDriver })
      notify('success', `${result.status.product} is ready`, result.steps.join(' '))
      for (const note of result.notes ?? []) notify('info', 'Worth knowing', note)
      await refresh()
    } catch (cause) {
      notify('error', 'Setup failed', (cause as Error).message)
    }
  }, [notify, refresh])

  const ready = status?.ready ?? false
  const principal = authInfo?.enabled ? authInfo.principal : null
  const canWrite = !principal || principal.role === 'admin'
  const running = containers?.filter((c) => c.status === 'Running').length ?? 0

  return (
    <AuthContext.Provider value={authInfo}>
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <img src="/lemon.svg" alt="" />
          <span>lemondx</span>
        </div>
        {status && (
          <div className="topbar-meta">
            <span>{status.product} {status.server_version}</span>
            <span className="faint">·</span>
            <span>{containers?.length ?? 0} container{containers?.length === 1 ? '' : 's'}</span>
            {running > 0 && (
              <>
                <span className="faint">·</span>
                <span style={{ color: 'var(--ok)' }}>{running} running</span>
              </>
            )}
          </div>
        )}
        <nav className="topbar-nav" aria-label="Views">
          {VIEWS
            .map((id) => (
              <button
                key={id}
                className="topbar-nav-item"
                aria-current={view === id}
                onClick={() => setView(id)}
              >
                {id[0].toUpperCase() + id.slice(1)}
              </button>
            ))}
        </nav>

        <div className="topbar-spacer" />
        <div className="topbar-actions">
          <button className="btn btn-ghost btn-icon" onClick={() => refresh()} title="Refresh"
            aria-label="Refresh now">
            <RefreshIcon />
          </button>
          <button className="btn btn-ghost btn-icon" onClick={toggle}
            title={`Switch to ${theme === 'dark' ? 'light' : 'dark'} mode`}
            aria-label={`Switch to ${theme === 'dark' ? 'light' : 'dark'} mode`}>
            {theme === 'dark' ? <SunIcon /> : <MoonIcon />}
          </button>
          {principal && (
            <span className="topbar-user" title={`Signed in via ${principal.via}`}>
              <span>{principal.name}</span>
              {principal.role !== 'admin' && <span className="badge badge-dim">read-only</span>}
              {/* Proxy identity is not ours to end; the proxy owns that session. */}
              {principal.via !== 'proxy' && (
                <button className="btn btn-ghost btn-sm" onClick={logout}>Log out</button>
              )}
            </span>
          )}
          {view === 'containers' && (
            <button className="btn btn-primary" onClick={() => setShowCreate(true)}
              disabled={!ready || !canWrite}>
              <PlusIcon /> New
            </button>
          )}
        </div>
      </header>

      {/* Keyed by identity: views load their data on mount, and nothing loaded
          as one user should still be on screen after logging in as another. */}
      <main className="main" key={principal ? `${principal.name}/${principal.role}` : '-'}>
        {connectionError && (
          <div className="banner banner-error">
            <div className="banner-body">
              <h3>Lost contact with the lemondx server</h3>
              <p style={{ margin: 0 }}>{connectionError}</p>
            </div>
          </div>
        )}

        {principal && !canWrite && (
          <div className="banner">
            <div className="banner-body">
              <h3>Read-only access</h3>
              <p style={{ margin: 0 }}>
                You can look around, but {principal.name} cannot make changes here.
              </p>
            </div>
          </div>
        )}

        {status && !status.ready && canWrite && (
          <SetupBanner status={status} onSetup={runSetup} />
        )}

        {view === 'access' ? (
          <AccessView onNotify={notify} />
        ) : view === 'nodes' ? (
          <NodesView onNotify={notify} onMembershipChanged={reloadCluster} />
        ) : view === 'modules' ? (
          <ModulesView onNotify={notify} />
        ) : view === 'templates' ? (
          <TemplatesView
            containers={containers}
            localNode={localNode}
            scope={scope}
            runs={templateRuns}
            onRunStarted={templateRunStarted}
            ready={ready}
            onNotify={notify}
            onOpen={openInstance}
            // These add and remove instances but change none the table shows
            // optimistically, so polling carries on and the table follows along.
            onChanged={(removed) => {
              // A recreated instance comes back under the same name, but the
              // drawer would be showing the old one. Matched by `keyOf`, so
              // destroying one node's `web-1` does not shut the drawer on
              // another node's.
              setSelected((current) => (
                current && removed.includes(keyOf(current)) ? null : current))
              refresh()
            }}
          />
        ) : view === 'storage' ? (
          <>
            <div className="section-head">
              <h2>Storage</h2>
              <span className="faint" style={{ fontSize: 12.5 }}>
                local pools, capacity and volumes
              </span>
            </div>
            <StorageView onNotify={notify} />
          </>
        ) : view === 'resources' ? (
          <>
            <div className="section-head">
              <h2>Resources</h2>
              <span className="faint" style={{ fontSize: 12.5 }}>
                what instances have claimed against what this host has
              </span>
            </div>
            <ResourcesView />
          </>
        ) : view === 'network' ? (
          <>
            <div className="section-head">
              <h2>Network</h2>
              <span className="faint" style={{ fontSize: 12.5 }}>
                how {status?.product ?? 'the daemon'} wires up container networking
              </span>
            </div>
            <NetworkView onNotify={notify} />
          </>
        ) : (
        <>
        <div className="section-head">
          <h2>Containers</h2>
          {status?.storage_pools.length && scope.kind === 'local' ? (
            <span className="faint" style={{ fontSize: 12.5 }}>
              pool {(status.root_pool ?? status.storage_pools[0]).name}{' '}
              ({(status.root_pool ?? status.storage_pools[0]).driver})
              {status.default_network ? ` · ${status.default_network}` : ''}
            </span>
          ) : null}
          {federated && (
            <>
              <div className="topbar-spacer" />
              <ScopePicker scope={scope} nodes={nodes} groups={groups}
                onChange={chooseScope} />
            </>
          )}
        </div>

        {/* A node that did not answer means the table is showing less than was
            asked for, which it should say rather than quietly shrink. */}
        {scopeErrors.length > 0 && (
          <div className="banner banner-warn">
            <div className="banner-body">
              <h3>{scopeErrors.length} node(s) did not answer</h3>
              <p style={{ margin: 0 }}>
                {scopeErrors.map((e) => `${e.node}: ${e.error}`).join('; ')}
              </p>
            </div>
          </div>
        )}

        {containers === null && !connectionError ? (
          <div className="card">
            <div className="loading-wrap"><span className="spinner" /> Loading containers…</div>
          </div>
        ) : (
          <ContainerTable
            containers={containers ?? []}
            creates={creates}
            health={health}
            selected={selected ? keyOf(selected) : null}
            busy={busy}
            canCreate={ready}
            localNode={localNode}
            nodeUrls={nodeUrls}
            showNodes={scope.kind !== 'local'}
            onSelect={setSelected}
            onAction={changeState}
            onBulkAction={bulkState}
            onDelete={setPendingDelete}
            onCreate={() => setShowCreate(true)}
          />
        )}
        </>
        )}
      </main>

      {selected && (
        <ContainerDrawer
          // Remounts on every switch, so per-container UI state (the active
          // tab, an in-progress limits edit, a half-typed snapshot name)
          // never leaks from one container into another.
          key={keyOf(selected)}
          name={selected.name}
          // Undefined for this node, so its calls take the plain path; any
          // other name routes every one of them through that node.
          node={selected.node && selected.node !== localNode ? selected.node : undefined}
          health={selected.node && selected.node !== localNode
            ? null : health[selected.name] ?? null}
          busy={!!busy[keyOf(selected)]}
          refreshToken={refreshToken}
          onClose={() => setSelected(null)}
          // The drawer speaks in names; the record it acts on is the selected
          // one, which carries the node its calls have to reach.
          onAction={(_, action) => changeState(selected, action)}
          onDelete={() => setPendingDelete(selected)}
          onNotify={notify}
        />
      )}

      {gate && <LoginGate info={gate} onDone={signedIn} />}

      {showCreate && (
        <CreateDialog onCancel={() => setShowCreate(false)} onCreate={create}
          onNotify={notify} />
      )}

      {pendingDelete && (
        <ConfirmDialog
          title={`Delete ${pendingDelete.name}${
            pendingDelete.node && pendingDelete.node !== localNode
              ? ` on ${pendingDelete.node}` : ''}?`}
          message={`This permanently removes the container, its filesystem and all of its snapshots. If it is running it will be stopped first. This cannot be undone.`}
          confirmLabel="Delete"
          danger
          busy={deleting}
          onConfirm={confirmDelete}
          onCancel={() => setPendingDelete(null)}
        />
      )}

      <Toasts toasts={toasts} onDismiss={dismiss} />
    </div>
    </AuthContext.Provider>
  )
}
