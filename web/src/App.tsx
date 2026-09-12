import { useCallback, useEffect, useRef, useState } from 'react'
import { ApiError, api } from './lib/api'
import type { Container, CreateRequest, StateAction, Status } from './lib/types'
import { useTheme } from './hooks/useTheme'
import { useToasts } from './hooks/useToasts'
import type { ToastKind } from './hooks/useToasts'
import { ConfirmDialog } from './components/ConfirmDialog'
import { ContainerDrawer } from './components/ContainerDrawer'
import { ContainerTable } from './components/ContainerTable'
import { CreateDialog } from './components/CreateDialog'
import { MoonIcon, PlusIcon, RefreshIcon, SunIcon } from './components/Icons'
import { SetupBanner } from './components/SetupBanner'
import { TokenGate } from './components/TokenGate'
import { Toasts } from './components/Toasts'

const POLL_INTERVAL = 3000

export default function App() {
  const { theme, toggle } = useTheme()
  const { toasts, push, dismiss } = useToasts()

  const [status, setStatus] = useState<Status | null>(null)
  const [containers, setContainers] = useState<Container[] | null>(null)
  const [connectionError, setConnectionError] = useState<string | null>(null)
  const [busy, setBusy] = useState<Record<string, boolean>>({})
  const [selected, setSelected] = useState<string | null>(null)
  const [showCreate, setShowCreate] = useState(false)
  const [pendingDelete, setPendingDelete] = useState<string | null>(null)
  const [deleting, setDeleting] = useState(false)
  const [refreshToken, setRefreshToken] = useState(0)
  const [needsToken, setNeedsToken] = useState(false)

  // Any in-flight mutation pauses polling so it cannot clobber optimistic state.
  const mutating = useRef(0)

  const refresh = useCallback(async (signal?: AbortSignal) => {
    try {
      const [nextStatus, nextContainers] = await Promise.all([
        api.status(signal),
        api.listContainers(signal),
      ])
      setStatus(nextStatus)
      setContainers(nextContainers)
      setConnectionError(null)
    } catch (cause) {
      if ((cause as Error).name === 'AbortError') return
      if (cause instanceof ApiError && cause.status === 401) {
        setNeedsToken(true)
        return
      }
      setConnectionError((cause as Error).message)
    }
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

  const notify = useCallback(
    (kind: ToastKind, title: string, detail?: string) => push(kind, title, detail),
    [push],
  )

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
    mutating.current += 1
    try {
      await api.createContainer(request)
      setShowCreate(false)
      notify('success', `Created ${request.name}`,
        request.start ? 'It is starting up now.' : 'It was created but not started.')
    } finally {
      mutating.current -= 1
      await refresh()
    }
  }, [notify, refresh])

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
  const running = containers?.filter((c) => c.status === 'Running').length ?? 0

  return (
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
          <button className="btn btn-primary" onClick={() => setShowCreate(true)}
            disabled={!ready}>
            <PlusIcon /> New
          </button>
        </div>
      </header>

      <main className="main">
        {connectionError && (
          <div className="banner banner-error">
            <div className="banner-body">
              <h3>Lost contact with the lemondx server</h3>
              <p style={{ margin: 0 }}>{connectionError}</p>
            </div>
          </div>
        )}

        {status && !status.ready && (
          <SetupBanner status={status} onSetup={runSetup} />
        )}

        <div className="section-head">
          <h2>Containers</h2>
          {status?.storage_pools.length ? (
            <span className="faint" style={{ fontSize: 12.5 }}>
              pool {status.storage_pools[0].name} ({status.storage_pools[0].driver})
              {status.networks[0] ? ` · ${status.networks[0].name}` : ''}
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
            selected={selected}
            busy={busy}
            canCreate={ready}
            onSelect={setSelected}
            onAction={changeState}
            onDelete={setPendingDelete}
            onCreate={() => setShowCreate(true)}
          />
        )}
      </main>

      {selected && (
        <ContainerDrawer
          name={selected}
          busy={!!busy[selected]}
          refreshToken={refreshToken}
          onClose={() => setSelected(null)}
          onAction={changeState}
          onDelete={setPendingDelete}
          onNotify={notify}
        />
      )}

      {needsToken && (
        <TokenGate onSubmit={() => { setNeedsToken(false); refresh() }} />
      )}

      {showCreate && (
        <CreateDialog onCancel={() => setShowCreate(false)} onCreate={create} />
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
  )
}
