import { useCallback, useEffect, useRef, useState } from 'react'
import { ApiError, api, hasToken, setToken } from './lib/api'
import type {
  AuthInfo, Container, CreateProgress, CreateRequest, HealthRecord, HealthStatus, StateAction,
  Status, TemplateRun,
} from './lib/types'
import { AuthContext } from './hooks/useAuth'
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
import { ResourcesView } from './components/ResourcesView'
import { SetupBanner } from './components/SetupBanner'
import { StorageView } from './components/StorageView'
import { LoginGate } from './components/LoginGate'
import { TemplatesView } from './components/TemplatesView'
import { Toasts } from './components/Toasts'

const POLL_INTERVAL = 3000

const VIEWS = ['containers', 'templates', 'resources', 'storage', 'network', 'modules', 'access'] as const

export default function App() {
  const { theme, toggle } = useTheme()
  const { toasts, push, dismiss } = useToasts()

  const [status, setStatus] = useState<Status | null>(null)
  const [containers, setContainers] = useState<Container[] | null>(null)
  const [creates, setCreates] = useState<CreateProgress[]>([])
  const [templateRuns, setTemplateRuns] = useState<TemplateRun[]>([])
  const [health, setHealth] = useState<Record<string, HealthRecord>>({})
  const [connectionError, setConnectionError] = useState<string | null>(null)
  const [busy, setBusy] = useState<Record<string, boolean>>({})
  const [selected, setSelected] = useState<string | null>(null)
  const [showCreate, setShowCreate] = useState(false)
  const [pendingDelete, setPendingDelete] = useState<string | null>(null)
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
    try {
      const [nextStatus, nextContainers, nextCreates, nextRuns, nextHealth] = await Promise.all([
        api.status(signal),
        api.listContainers(signal),
        // Progress is extra; a failure here must not look like losing the server.
        api.creates(signal).catch(() => null),
        api.templateRuns(signal).catch(() => null),
        // Read from the server's memory, so polling it this often is free; the
        // checks themselves run on the server's own interval.
        api.health(signal).catch(() => null),
      ])
      if (sequence !== refreshSequence.current) return
      setStatus(nextStatus)
      setContainers(nextContainers)
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
  }, [reportCreates, reportRuns, reportHealth])

  useEffect(() => {
    const controller = new AbortController()
    api.authInfo(controller.signal).then(setAuthInfo).catch(() => {})
    return () => controller.abort()
  }, [])

  const signedIn = useCallback(async (info: AuthInfo | null) => {
    setGate(null)
    // A pasted token passes null: ask who it makes us.
    setAuthInfo(info ?? await api.authInfo().catch(() => null))
    refresh()
  }, [refresh])

  const logout = useCallback(async () => {
    setToken(null)
    await api.logout().catch(() => {})
    const info = await api.authInfo().catch(() => null)
    setAuthInfo(info)
    if (info?.enabled && !info.principal) setGate(info)
  }, [])

  useEffect(() => {
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
  }, [refresh])

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

  const changeState = useCallback((name: string, action: StateAction) => {
    const verb = { start: 'Started', stop: 'Stopped', restart: 'Restarted',
      freeze: 'Paused', unfreeze: 'Resumed' }[action]
    return mutate(
      name,
      () => api.setState(name, action).then(() => {}),
      () => notify('success', `${verb} ${name}`),
      (message) => notify('error', `Could not ${action} ${name}`, message),
    )
  }, [mutate, notify])

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
    const name = pendingDelete
    if (!name) return
    setDeleting(true)
    // force also stops a running container first.
    await mutate(
      name,
      () => api.deleteContainer(name, true).then(() => {}),
      () => {
        notify('success', `Deleted ${name}`)
        setSelected((current) => (current === name ? null : current))
      },
      (message) => notify('error', `Could not delete ${name}`, message),
    )
    setDeleting(false)
    setPendingDelete(null)
  }, [mutate, notify, pendingDelete])

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
        ) : view === 'modules' ? (
          <ModulesView onNotify={notify} />
        ) : view === 'templates' ? (
          <TemplatesView
            containers={containers}
            runs={templateRuns}
            onRunStarted={templateRunStarted}
            ready={ready}
            onNotify={notify}
            onOpen={setSelected}
            // These add and remove instances but change none the table shows
            // optimistically, so polling carries on and the table follows along.
            onChanged={(removed) => {
              // A recreated instance comes back under the same name, but the
              // drawer would be showing the old one.
              setSelected((current) => (current && removed.includes(current) ? null : current))
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
          {status?.storage_pools.length ? (
            <span className="faint" style={{ fontSize: 12.5 }}>
              pool {(status.root_pool ?? status.storage_pools[0]).name}{' '}
              ({(status.root_pool ?? status.storage_pools[0]).driver})
              {status.default_network ? ` · ${status.default_network}` : ''}
            </span>
          ) : null}
        </div>

        {containers === null && !connectionError ? (
          <div className="card">
            <div className="loading-wrap"><span className="spinner" /> Loading containers…</div>
          </div>
        ) : (
          <ContainerTable
            containers={containers ?? []}
            creates={creates}
            health={health}
            selected={selected}
            busy={busy}
            canCreate={ready}
            onSelect={setSelected}
            onAction={changeState}
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
          key={selected}
          name={selected}
          health={health[selected] ?? null}
          busy={!!busy[selected]}
          refreshToken={refreshToken}
          onClose={() => setSelected(null)}
          onAction={changeState}
          onDelete={setPendingDelete}
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
          title={`Delete ${pendingDelete}?`}
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
