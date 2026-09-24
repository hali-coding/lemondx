import { useCallback, useEffect, useRef, useState } from 'react'
import { useCanOperate, useCanWrite } from '../hooks/useAuth'
import { api } from '../lib/api'
import { absoluteTime, bytes, cpuTime, relativeTime, secondsAgo } from '../lib/format'
import type { ContainerDetail, HealthRecord, StateAction } from '../lib/types'
import { CameraIcon, CloseIcon, PauseIcon, PlayIcon, RestartIcon, StopIcon, TrashIcon } from './Icons'
import { BootstrapPanel } from './BootstrapPanel'
import { ExecConsole } from './ExecConsole'
import { AppCheckLabel, HealthLabel } from './HealthDot'
import { StatusBadge } from './StatusBadge'
import { staleSummary } from '../lib/stale'

type Tab = 'overview' | 'snapshots' | 'bootstrap' | 'console'

interface Props {
  name: string
  /**
   * The node it lives on, when that is not this one. Every call the drawer
   * makes is routed there, so an instance is managed from wherever you are
   * looking rather than only from its own host.
   */
  node?: string
  /** The server's latest check, or null when it has none (stopped, or checks off). */
  health: HealthRecord | null
  /** Its node checks health, so no record yet means not checked yet. */
  healthPending: boolean
  busy: boolean
  onClose: () => void
  onAction: (name: string, action: StateAction) => void
  onDelete: (name: string) => void
  onNotify: (kind: 'success' | 'error', title: string, detail?: string) => void
  /** Open the full output of this instance's app check. */
  onShowAppCheck: () => void
  refreshToken: number
}

export function ContainerDrawer({
  name, node, health, healthPending, busy, onClose, onAction, onDelete, onNotify, onShowAppCheck, refreshToken,
}: Props) {
  const canWrite = useCanWrite()
  const canOperate = useCanOperate()
  const [detail, setDetail] = useState<ContainerDetail | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [tab, setTab] = useState<Tab>('overview')
  const [snapshotName, setSnapshotName] = useState('')
  const [snapBusy, setSnapBusy] = useState(false)
  const [editingLimits, setEditingLimits] = useState(false)
  const [limitCpu, setLimitCpu] = useState('')
  const [limitMemory, setLimitMemory] = useState('')
  const [savingLimits, setSavingLimits] = useState(false)

  const load = useCallback((signal?: AbortSignal) => {
    api.getContainer(name, signal, node)
      .then((data) => { setDetail(data); setError(null) })
      .catch((cause) => {
        if ((cause as Error).name !== 'AbortError') setError((cause as Error).message)
      })
  }, [name, node])

  useEffect(() => {
    const controller = new AbortController()
    load(controller.signal)
    return () => controller.abort()
  }, [load, refreshToken])

  // onClose is rebuilt by the parent on every poll, so read it through a ref
  // and bind the listener once.
  const closeRef = useRef(onClose)
  useEffect(() => {
    closeRef.current = onClose
  }, [onClose])

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') closeRef.current()
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [])

  async function createSnapshot(event: React.FormEvent) {
    event.preventDefault()
    const trimmed = snapshotName.trim()
    if (!trimmed) return
    setSnapBusy(true)
    try {
      await api.createSnapshot(name, trimmed, false, node)
      setSnapshotName('')
      onNotify('success', `Snapshot “${trimmed}” created`)
      load()
    } catch (cause) {
      onNotify('error', 'Could not create snapshot', (cause as Error).message)
    } finally {
      setSnapBusy(false)
    }
  }

  function startEditLimits() {
    if (!detail) return
    setLimitCpu(detail.limits.cpu)
    setLimitMemory(detail.limits.memory)
    setEditingLimits(true)
  }

  async function saveLimits(event: React.FormEvent) {
    event.preventDefault()
    setSavingLimits(true)
    try {
      // Sending "" (not omitting the key) is how the API clears a limit, so
      // both fields go over every time, blank or not -- see update_limits()
      // in service.py.
      await api.updateLimits(name, { cpu: limitCpu.trim(), memory: limitMemory.trim() }, node)
      onNotify('success', `Updated limits for ${name}`)
      setEditingLimits(false)
      load()
    } catch (cause) {
      onNotify('error', 'Could not update limits', (cause as Error).message)
    } finally {
      setSavingLimits(false)
    }
  }

  async function runSnapshotAction(
    action: 'restore' | 'delete', snapshot: string,
  ) {
    setSnapBusy(true)
    try {
      if (action === 'restore') {
        await api.restoreSnapshot(name, snapshot, node)
        onNotify('success', `Restored “${snapshot}”`)
      } else {
        await api.deleteSnapshot(name, snapshot, node)
        onNotify('success', `Deleted snapshot “${snapshot}”`)
      }
      load()
    } catch (cause) {
      onNotify('error', `Could not ${action} snapshot`, (cause as Error).message)
    } finally {
      setSnapBusy(false)
    }
  }

  const running = detail?.status === 'Running'
  const frozen = detail?.status === 'Frozen'

  return (
    <>
      <div className="scrim" onClick={onClose} />
      <aside className="drawer" role="dialog" aria-modal="true" aria-label={`${name} details`}>
        <div className="drawer-head">
          <h2 className="truncate">{name}</h2>
          {detail && !busy && <StatusBadge status={detail.status} />}
          {busy && <span className="badge badge-warn"><span className="spinner" />working</span>}
          <button className="btn btn-ghost btn-icon" onClick={onClose} aria-label="Close details">
            <CloseIcon />
          </button>
        </div>

        <div className="tabs" role="tablist">
          {(['overview', 'snapshots', 'bootstrap', 'console'] as Tab[]).map((id) => (
            <button
              key={id}
              className="tab"
              role="tab"
              aria-selected={tab === id}
              onClick={() => setTab(id)}
            >
              {id === 'overview' ? 'Overview'
                : id === 'snapshots' ? `Snapshots${detail?.snapshots.length ? ` (${detail.snapshots.length})` : ''}`
                : id === 'bootstrap' ? 'Bootstrap'
                : 'Console'}
            </button>
          ))}
        </div>

        <div className="drawer-body">
          {error && (
            <div className="banner banner-error">
              <div className="banner-body"><p style={{ margin: 0 }}>{error}</p></div>
            </div>
          )}

          {!detail && !error && (
            <div className="loading-wrap"><span className="spinner" /> Loading…</div>
          )}

          {detail && tab === 'overview' && (
            <>
              {detail.stale?.length > 0 && (
                <div className="banner banner-warn">
                  <div className="banner-body">
                    <h3>Stale</h3>
                    <p style={{ margin: 0 }}>
                      {staleSummary(detail.stale, detail.template, detail.stack)}
                    </p>
                  </div>
                </div>
              )}
              {!health && healthPending && detail.status === 'Running' && (
                <div className="panel">
                  <h3>Health</h3>
                  <div className="app-check" style={{ marginTop: 0 }}>
                    <span className="health-label health-dim">
                      <span className="health-dot" aria-hidden="true" />pending
                    </span>
                    <span className="faint">App</span>
                    <span className="health-label health-dim">
                      <span className="app-dot" aria-hidden="true" />pending
                    </span>
                    <span className="faint" style={{ fontSize: 12.5 }}>
                      not checked yet; the next round will
                    </span>
                  </div>
                </div>
              )}
              {health && detail.status === 'Running' && (
                <div className="panel">
                  <h3>Health</h3>
                  <div>
                    <HealthLabel record={health} />
                    <span className="faint" style={{ marginLeft: 8, fontSize: 12.5 }}>
                      since {secondsAgo(health.since)} · checked {secondsAgo(health.checked_at)}
                      {health.probe?.ok ? ` · answered in ${health.probe.ms} ms` : ''}
                    </span>
                  </div>
                  {health.reasons.length > 0 && (
                    <ul className="health-reasons">
                      {health.reasons.map((reason) => <li key={reason}>{reason}</li>)}
                    </ul>
                  )}
                  {health.app && (
                    <div className="app-check">
                      <span className="faint">App</span>
                      <AppCheckLabel app={health.app} />
                      <span className="faint" style={{ fontSize: 12.5 }}>
                        {appCheckDetail(health.app)}
                      </span>
                      {health.app.configured && (
                        <button type="button" className="btn btn-sm" onClick={onShowAppCheck}>
                          Output
                        </button>
                      )}
                    </div>
                  )}
                </div>
              )}

              <div className="panel">
                <h3>Resources</h3>
                <div className="stat-grid">
                  <div className="stat">
                    <div className="stat-label">Memory</div>
                    <div className="stat-value">{bytes(detail.memory_usage)}</div>
                  </div>
                  {health?.cpu && (
                    <div className="stat" title={`${health.cpu.cores_used} of ${health.cpu.cores} CPU(s), averaged since the previous check`}>
                      <div className="stat-label">CPU</div>
                      <div className="stat-value">{health.cpu.percent}%</div>
                    </div>
                  )}
                  {health?.load && (
                    <div className="stat" title={loadTitle(health.load)}>
                      <div className="stat-label">Load</div>
                      <div className={`stat-value${health.status === 'degraded'
                        && health.load.scope === 'instance' ? ' health-warn' : ''}`}>
                        {health.load.scope === 'instance'
                          ? health.load.avg[0].toFixed(2)
                          : <span className="faint" style={{ fontSize: 13 }}>host-wide</span>}
                      </div>
                    </div>
                  )}
                  <div className="stat">
                    <div className="stat-label">CPU time</div>
                    <div className="stat-value">{cpuTime(detail.cpu_time_ns)}</div>
                  </div>
                  <div className="stat">
                    <div className="stat-label">Processes</div>
                    <div className="stat-value">{detail.processes || '—'}</div>
                  </div>
                  <div className="stat">
                    <div className="stat-label">Net in</div>
                    <div className="stat-value">{bytes(detail.network_rx)}</div>
                  </div>
                  <div className="stat">
                    <div className="stat-label">Net out</div>
                    <div className="stat-value">{bytes(detail.network_tx)}</div>
                  </div>
                </div>
              </div>

              <div className="panel">
                <h3>Details</h3>
                <dl className="kv">
                  <dt>Type</dt>
                  <dd>{detail.type === 'virtual-machine' ? 'Virtual machine' : 'Container'}</dd>
                  <dt>Image</dt><dd>{detail.image || detail.image_alias || '—'}</dd>
                  <dt>Architecture</dt><dd>{detail.architecture ?? '—'}</dd>
                  <dt>Created</dt>
                  <dd title={absoluteTime(detail.created_at)}>{relativeTime(detail.created_at)}</dd>
                  <dt>Last used</dt>
                  <dd title={absoluteTime(detail.last_used_at)}>{relativeTime(detail.last_used_at)}</dd>
                  <dt>Profiles</dt><dd>{detail.profiles.join(', ') || '—'}</dd>
                  {editingLimits ? (
                    <>
                      <dt>CPU limit</dt>
                      <dd>
                        <input
                          className="input" style={{ maxWidth: 140 }}
                          value={limitCpu} disabled={savingLimits}
                          placeholder="e.g. 2" autoComplete="off"
                          aria-label="CPU limit"
                          onChange={(e) => setLimitCpu(e.target.value)}
                        />
                      </dd>
                      <dt>Memory limit</dt>
                      <dd>
                        <input
                          className="input" style={{ maxWidth: 140 }}
                          value={limitMemory} disabled={savingLimits}
                          placeholder="e.g. 2GiB" autoComplete="off"
                          aria-label="Memory limit"
                          onChange={(e) => setLimitMemory(e.target.value)}
                        />
                      </dd>
                      <dt />
                      <dd>
                        <div className="btn-group">
                          <button className="btn btn-sm btn-primary" disabled={savingLimits}
                            onClick={saveLimits}>
                            {savingLimits && <span className="spinner" />}
                            Save
                          </button>
                          <button className="btn btn-sm" disabled={savingLimits}
                            onClick={() => setEditingLimits(false)}>
                            Cancel
                          </button>
                        </div>
                        <span className="hint" style={{ display: 'block', marginTop: 6 }}>
                          Leave blank for no limit. CPU is a core count; memory takes a
                          unit (<span className="mono">2GiB</span>,{' '}
                          <span className="mono">512MiB</span>) — a bare number is read
                          as GiB.
                        </span>
                      </dd>
                    </>
                  ) : (
                    <>
                      <dt>CPU limit</dt><dd>{detail.limits.cpu || 'unlimited'}</dd>
                      <dt>Memory limit</dt>
                      <dd>
                        {detail.limits.memory || 'unlimited'}
                        <button
                          className="btn btn-ghost btn-sm"
                          style={{ marginLeft: 8, padding: '1px 7px', fontSize: 11 }}
                          disabled={!canWrite}
                          onClick={startEditLimits}
                        >
                          Edit
                        </button>
                      </dd>
                    </>
                  )}
                  {detail.ephemeral && <><dt>Ephemeral</dt><dd>deleted when stopped</dd></>}
                </dl>
              </div>

              <div className="panel">
                <h3>Network</h3>
                {(() => {
                  // The NIC's parent network is the single most useful fact
                  // here: it says which bridge the container is actually on.
                  const nic = Object.entries(detail.expanded_devices)
                    .find(([, device]) => device.type === 'nic')
                  const parent = nic?.[1].network || nic?.[1].parent
                  return parent ? (
                    <p className="dim" style={{ fontSize: 12.5, margin: '0 0 8px' }}>
                      Attached to <span className="mono">{parent}</span> via{' '}
                      <span className="mono">{nic?.[0]}</span>
                      {nic?.[1].nictype && <> ({nic[1].nictype})</>} — see the{' '}
                      <strong>Network</strong> view for how that bridge is set up.
                    </p>
                  ) : null
                })()}
                {detail.network_detail.filter((i) => i.name !== 'lo').length === 0 ? (
                  <p className="dim" style={{ fontSize: 13, margin: 0 }}>
                    No interfaces — the container is not running.
                  </p>
                ) : (
                  detail.network_detail.filter((i) => i.name !== 'lo').map((iface) => (
                    <div className="list-row" key={iface.name}>
                      <div className="list-row-main">
                        <strong>{iface.name}</strong>{' '}
                        <span className="faint mono">{iface.hwaddr}</span>
                        <div className="mono dim" style={{ fontSize: 12, marginTop: 2 }}>
                          {iface.addresses.join(', ') || 'no address'}
                        </div>
                      </div>
                      <div className="dim num" style={{ fontSize: 12, textAlign: 'right' }}>
                        ↓ {bytes(iface.rx)}<br />↑ {bytes(iface.tx)}
                      </div>
                    </div>
                  ))
                )}
              </div>
            </>
          )}

          {detail && tab === 'snapshots' && (
            <>
              <form className="console-form" onSubmit={createSnapshot} style={{ marginBottom: 14 }}>
                <input
                  className="input"
                  value={snapshotName}
                  onChange={(event) => setSnapshotName(event.target.value)}
                  placeholder="snapshot name"
                  aria-label="New snapshot name"
                  autoComplete="off"
                  disabled={snapBusy || !canWrite}
                />
                <button className="btn btn-primary" type="submit"
                  disabled={snapBusy || !canWrite || !snapshotName.trim()}>
                  <CameraIcon /> Take
                </button>
              </form>

              {detail.snapshots.length === 0 ? (
                <p className="dim" style={{ fontSize: 13 }}>
                  No snapshots yet. A snapshot captures the filesystem so you can roll back later.
                </p>
              ) : (
                detail.snapshots.map((snapshot) => (
                  <div className="list-row" key={snapshot.name}>
                    <div className="list-row-main">
                      <strong>{snapshot.name}</strong>
                      <div className="faint" style={{ fontSize: 12 }}
                        title={absoluteTime(snapshot.created_at)}>
                        {relativeTime(snapshot.created_at)}
                        {snapshot.stateful ? ' · stateful' : ''}
                      </div>
                    </div>
                    <button className="btn btn-sm" disabled={snapBusy || !canWrite}
                      onClick={() => runSnapshotAction('restore', snapshot.name)}>
                      Restore
                    </button>
                    <button className="btn btn-sm btn-icon btn-danger" disabled={snapBusy || !canWrite}
                      aria-label={`Delete snapshot ${snapshot.name}`}
                      onClick={() => runSnapshotAction('delete', snapshot.name)}>
                      <TrashIcon />
                    </button>
                  </div>
                ))
              )}
            </>
          )}

          {detail && tab === 'bootstrap' && (
            <BootstrapPanel key={name} name={name} node={node} running={running}
              onFinished={load} />
          )}

          {detail && tab === 'console' && (
            /* key: remount per container so output never leaks across them */
            <ExecConsole key={name} name={name} node={node} running={running} />
          )}
        </div>

        <div className="drawer-actions">
          {running || frozen ? (
            <>
              <button className="btn" disabled={busy || !canOperate} onClick={() => onAction(name, 'stop')}>
                <StopIcon /> Stop
              </button>
              <button className="btn" disabled={busy || !canOperate} onClick={() => onAction(name, 'restart')}>
                <RestartIcon /> Restart
              </button>
              <button className="btn" disabled={busy || !canOperate}
                onClick={() => onAction(name, frozen ? 'unfreeze' : 'freeze')}>
                {frozen ? <PlayIcon /> : <PauseIcon />} {frozen ? 'Resume' : 'Pause'}
              </button>
            </>
          ) : (
            <button className="btn btn-primary" disabled={busy || !canOperate}
              onClick={() => onAction(name, 'start')}>
              <PlayIcon /> Start
            </button>
          )}
          <div style={{ flex: 1 }} />
          <button className="btn btn-danger" disabled={busy || !canOperate} onClick={() => onDelete(name)}>
            <TrashIcon /> Delete
          </button>
        </div>
      </aside>
    </>
  )
}

function appCheckDetail(app: NonNullable<HealthRecord['app']>): string {
  if (!app.configured) return 'no app check configured'
  if (app.status === 'pending') return `from template ${app.template} · waiting for its first run`
  const parts = [app.output || `exit ${app.code ?? '?'}`]
  if (app.checked_at) parts.push(`ran ${secondsAgo(app.checked_at)}`)
  if (app.interval) parts.push(`every ${app.interval}s`)
  return parts.join(' · ')
}

function loadTitle(load: NonNullable<HealthRecord['load']>): string {
  if (load.scope !== 'instance') {
    return 'This container reports the host’s load average, and its cgroup could not be read to count its own.'
  }
  const [one, five, fifteen] = load.avg
  if (load.warming) return `Load average ${one.toFixed(2)}; still gathering its first minute of samples`
  if (five === null || fifteen === null) return `Load average ${one.toFixed(2)}`
  return `Load average: ${one.toFixed(2)} (1 min), ${five.toFixed(2)} (5 min), ${fifteen.toFixed(2)} (15 min)`
}
