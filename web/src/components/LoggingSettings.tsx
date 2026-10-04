import { useCallback, useEffect, useState } from 'react'
import { api, calls } from '../lib/api'
import { syncDetail, syncKind } from '../lib/sync'
import type {
  ClusterNode, LogDestination, LoggingSettings as Settings, LoggingStatus, LoggingTest, LogLevel,
  SyslogDestination,
} from '../lib/types'

interface Props {
  nodes: ClusterNode[]
  canWrite: boolean
  onNotify: (kind: 'success' | 'error' | 'info', title: string, detail?: string) => void
}

const LEVELS: { value: LogLevel; label: string }[] = [
  { value: 'debug', label: 'Debug — also every read and request; a lot' },
  { value: 'info', label: 'Info — every change, and who made it (recommended)' },
  { value: 'notice', label: 'Notice — logins, users, tokens, membership and settings' },
  { value: 'warning', label: 'Warning — only what was refused or failed' },
  { value: 'error', label: 'Error — only lemondx’s own faults' },
]

const FACILITIES = ['daemon', 'user', 'auth', 'local0', 'local1', 'local2', 'local3',
  'local4', 'local5', 'local6', 'local7']

interface EditorProps<D extends LogDestination> {
  value: D
  disabled: boolean
  onChange: (next: D) => void
}

/** A remote syslog host: where, over what, and under which facility. */
function SyslogEditor({ value, disabled, onChange }: EditorProps<SyslogDestination>) {
  return (
    <div className="logging-destination">
      <div className="field">
        <label>Host</label>
        <input className="input mono" value={value.host} disabled={disabled}
          placeholder="syslog.example.net" autoComplete="off"
          onChange={(e) => onChange({ ...value, host: e.target.value.trim() })} />
      </div>
      <div className="field">
        <label>Port</label>
        <input className="input mono" type="number" min={1} max={65535} value={value.port}
          disabled={disabled}
          onChange={(e) => onChange({ ...value, port: Number(e.target.value) || 514 })} />
      </div>
      <div className="field">
        <label>Protocol</label>
        <select className="select" value={value.protocol} disabled={disabled}
          onChange={(e) => onChange({ ...value, protocol: e.target.value as 'udp' | 'tcp' })}>
          <option value="udp">UDP</option>
          <option value="tcp">TCP</option>
        </select>
      </div>
      <div className="field">
        <label>Facility</label>
        <select className="select" value={value.facility} disabled={disabled}
          onChange={(e) => onChange({ ...value, facility: e.target.value })}>
          {FACILITIES.map((f) => <option key={f} value={f}>{f}</option>)}
        </select>
      </div>
    </div>
  )
}

/**
 * One editor per destination type, as `eventlog.DESTINATIONS` has one class
 * per type: another kind of log stream is an entry here and one there.
 */
const DESTINATION_EDITORS: {
  [K in LogDestination['type']]: {
    label: string
    blank: () => Extract<LogDestination, { type: K }>
    Editor: (props: EditorProps<Extract<LogDestination, { type: K }>>) => React.ReactElement
  }
} = {
  syslog: {
    label: 'Remote syslog',
    blank: () => ({ type: 'syslog', enabled: true, host: '', port: 514, protocol: 'udp',
      facility: 'local0' }),
    Editor: SyslogEditor,
  },
}

function describe(destination: LogDestination) {
  return `${destination.protocol.toUpperCase()} ${destination.host}:${destination.port}`
}

/**
 * The cluster's logging: one setting every member applies. Saved here, it is
 * pushed to each member like a template; each node then reports how its own
 * destinations are doing, since a collector one node reaches another may not.
 */
export function LoggingSettings({ nodes, canWrite, onNotify }: Props) {
  const [status, setStatus] = useState<LoggingStatus | null>(null)
  const [draft, setDraft] = useState<Settings | null>(null)
  const [perNode, setPerNode] = useState<Record<string, LoggingStatus | { error: string }>>({})
  const [busy, setBusy] = useState(false)
  const [test, setTest] = useState<LoggingTest | null>(null)
  const [error, setError] = useState<string | null>(null)
  const names = nodes.map((n) => n.name).join(',')

  const load = useCallback((signal?: AbortSignal, reset = false) => {
    api.logging(undefined, signal).then((next) => {
      setStatus(next)
      setError(null)
      if (reset) setDraft(next.settings)
    }).catch((cause) => {
      if ((cause as Error).name !== 'AbortError') setError((cause as Error).message)
    })
    for (const node of names ? names.split(',') : []) {
      const self = nodes.find((n) => n.name === node)?.self
      api.logging(self ? undefined : node, signal)
        .then((next) => setPerNode((all) => ({ ...all, [node]: next })))
        .catch((cause) => {
          if ((cause as Error).name !== 'AbortError') {
            setPerNode((all) => ({ ...all, [node]: { error: (cause as Error).message } }))
          }
        })
    }
  // `nodes` is read for `self` only; `names` is what changes the set asked.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [names])

  useEffect(() => {
    const controller = new AbortController()
    load(controller.signal, true)
    // Each node's status, on the same slow clock as the rest of the tab: a
    // collector going away shows up here without a reload.
    const timer = window.setInterval(() => load(controller.signal), 10_000)
    return () => { controller.abort(); window.clearInterval(timer) }
  }, [load])

  if (!draft || !status) {
    return (
      <section className="card access-card">
        <header className="access-head"><h3>Logging</h3></header>
        <div className="access-inset">
          {error ? <span className="field-error">{error}</span>
            : <div className="loading-wrap"><span className="spinner" /> Reading settings…</div>}
        </div>
      </section>
    )
  }

  const dirty = JSON.stringify(draft) !== JSON.stringify(status.settings)
  // A node that was off when the settings changed keeps its old ones: it holds
  // a copy, so reconciliation sees two versions and reports a conflict rather
  // than guess which is newer. Saving again is the push it missed.
  const behind = nodes.filter((n) => {
    const entry = perNode[n.name]
    return entry && !('error' in entry)
      && JSON.stringify(entry.settings) !== JSON.stringify(status.settings)
  }).map((n) => n.name)
  const badHost = draft.destinations.some((d) => d.enabled && !d.host)

  function setDestination(index: number, next: LogDestination | null) {
    setDraft((current) => current && {
      ...current,
      destinations: next === null
        ? current.destinations.filter((_, i) => i !== index)
        : current.destinations.map((d, i) => (i === index ? next : d)),
    })
  }

  async function save(settings = draft) {
    if (!settings) return
    setBusy(true)
    try {
      const saved = await api.saveLogging(settings)
      onNotify(syncKind(saved.synced), 'Logging settings saved',
        syncDetail(saved.synced) ?? 'Applied on this node.')
      setDraft(saved.value)
      load(undefined, true)
    } catch (cause) {
      onNotify('error', 'Could not save the logging settings', (cause as Error).message)
    } finally {
      setBusy(false)
    }
  }

  async function sendTest() {
    setBusy(true)
    setTest(null)
    try {
      setTest(await api.testLogging())
      load()
    } catch (cause) {
      onNotify('error', 'Could not send a test message', (cause as Error).message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="card access-card">
      <header className="access-head">
        <h3>Logging</h3>
        <span className="faint">
          Every change, who made it and how, on every node — one setting for the whole cluster.
        </span>
        <div className="row-actions access-head-end">
          {behind.length > 0 && !dirty && (
            <button className="btn btn-sm" disabled={busy || !canWrite}
              title={`${behind.join(', ')} ${behind.length === 1 ? 'has' : 'have'} older settings: push this node's again`}
              onClick={() => save(status.settings)}>
              Push to {behind.join(', ')}
            </button>
          )}
          <button className="btn btn-sm" disabled={busy || !canWrite || dirty}
            title={dirty ? 'Save first: the test uses the settings in force' : 'Send a test line to every destination of this node'}
            onClick={sendTest}>
            Send test message
          </button>
          <button className="btn btn-sm btn-primary" disabled={busy || !canWrite || !dirty || badHost}
            onClick={() => save()}
            title={calls.saveLogging(draft).path}>
            {busy && <span className="spinner" />}Save for the cluster
          </button>
        </div>
      </header>

      <div className="logging-body">
        <div className="field">
          <label htmlFor="log-level">Log level</label>
          <select id="log-level" className="select" value={draft.level} disabled={!canWrite}
            onChange={(e) => setDraft({ ...draft, level: e.target.value as LogLevel })}>
            {LEVELS.map((l) => <option key={l.value} value={l.value}>{l.label}</option>)}
          </select>
          <span className="hint">The lowest severity sent anywhere. Each line names the actor,
            the channel (ui, api, cli, peer, system) and a request id shared by everything
            that request did, on every node.</span>
        </div>

        <div className="field">
          <label htmlFor="log-local">Local syslog</label>
          <div className="logging-local">
            <span className="mono">{status.local_socket}</span>
            <select id="log-local" className="select" value={draft.local.facility}
              disabled={!canWrite} aria-label="Local facility"
              onChange={(e) => setDraft({ ...draft, local: { facility: e.target.value } })}>
              {FACILITIES.map((f) => <option key={f} value={f}>facility {f}</option>)}
            </select>
          </div>
          <span className="hint">
            Always on. Under systemd it is the journal: <span className="mono">journalctl -t lemondx</span>.
            {status.local_fallback && (
              <span style={{ color: 'var(--warn)' }}> This node has no syslog socket, so lines go to stderr.</span>
            )}
          </span>
        </div>

        {draft.destinations.map((destination, index) => {
          const kind = DESTINATION_EDITORS[destination.type]
          return (
            <div className="field" key={index}>
              <div className="logging-destination-head">
                <label className="check" style={{ fontWeight: 550 }}>
                  <input type="checkbox" checked={destination.enabled} disabled={!canWrite}
                    onChange={() => setDestination(index, { ...destination, enabled: !destination.enabled })} />
                  <span>{kind.label}</span>
                </label>
                <button type="button" className="btn btn-ghost btn-sm" disabled={!canWrite}
                  onClick={() => setDestination(index, null)}>
                  Remove
                </button>
              </div>
              <kind.Editor value={destination} disabled={!canWrite || !destination.enabled}
                onChange={(next) => setDestination(index, next)} />
              {destination.enabled && !destination.host && (
                <span className="field-error">A host is required.</span>
              )}
            </div>
          )
        })}
        {draft.destinations.length < 8 && (
          <button type="button" className="btn btn-ghost btn-sm" style={{ justifySelf: 'start' }}
            disabled={!canWrite}
            onClick={() => setDraft({ ...draft,
              destinations: [...draft.destinations, DESTINATION_EDITORS.syslog.blank()] })}>
            + Add a remote syslog host
          </button>
        )}
        {draft.destinations.length > 0 && (
          <span className="hint">
            UDP and TCP syslog travel unencrypted: names, addresses and what was done, never
            passwords, tokens or parameter values. Keep the collector on a trusted network.
          </span>
        )}

        {test && (
          <div className="logging-test">
            {test.results.map((r) => (
              <div key={r.label} className={r.ok ? '' : 'field-error'}>
                {r.ok ? '✓' : '✗'} {r.label}: {r.detail}
              </div>
            ))}
          </div>
        )}
      </div>

      {nodes.length > 1 && (
        <div className="table-scroll">
          <table className="ctable">
            <thead>
              <tr><th>Node</th><th>Level</th><th>Destinations</th></tr>
            </thead>
            <tbody>
              {nodes.map((node) => {
                const entry = perNode[node.name]
                if (!entry) {
                  return <tr key={node.name}><td>{node.name}</td><td colSpan={2} className="faint">…</td></tr>
                }
                if ('error' in entry) {
                  return (
                    <tr key={node.name}>
                      <td>{node.name}</td>
                      <td colSpan={2} className="field-error">{entry.error}</td>
                    </tr>
                  )
                }
                const late = behind.includes(node.name)
                return (
                  <tr key={node.name}>
                    <td>{node.name}{node.self ? <span className="faint"> (this node)</span> : null}</td>
                    <td>
                      {entry.settings.level}
                      {late && <span className="badge badge-warn" style={{ marginLeft: 6 }}
                        title="This node has older settings: it was unreachable when they were saved. Push to it from the button above.">behind</span>}
                    </td>
                    <td>
                      {entry.destinations.map((d) => (
                        <div key={d.label} className={d.error ? 'field-error' : undefined}>
                          {d.error ? '✗' : '✓'} {d.label}{d.error ? `: ${d.error}` : ''}
                        </div>
                      ))}
                      {entry.local_fallback && <div className="faint">no syslog socket: stderr</div>}
                      {entry.settings.destinations.filter((d) => d.enabled).length === 0
                        && <div className="faint">no remote</div>}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
      {nodes.length <= 1 && status.destinations.some((d) => d.error) && (
        <div className="access-inset">
          {status.destinations.filter((d) => d.error).map((d) => (
            <div key={d.label} className="field-error">✗ {d.label}: {d.error}</div>
          ))}
        </div>
      )}
      {draft.destinations.length > 0 && !dirty && (
        <div className="access-inset hint">
          Sending to {draft.destinations.filter((d) => d.enabled).map(describe).join(', ') || 'no remote host (all off)'}.
        </div>
      )}
    </section>
  )
}
