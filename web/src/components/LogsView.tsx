import { useEffect, useState } from 'react'
import { useLogTail } from '../hooks/useLogTail'
import type { TailScope } from '../hooks/useLogTail'
import type { ClusterNode, LogFilters, LogLevel, NodeGroup } from '../lib/types'
import { LogList } from './LogList'
import { PauseIcon, PlayIcon, TrashIcon } from './Icons'

interface Props {
  nodes: ClusterNode[]
  groups: NodeGroup[]
  federated: boolean
}

const KINDS = ['request', 'change', 'auth', 'system'] as const
const LEVELS: LogLevel[] = ['debug', 'info', 'notice', 'warning', 'error']

/** A value typed into a box, settled after a pause so each key does not restart the tail. */
function useSettled(value: string, ms = 400) {
  const [settled, setSettled] = useState(value)
  useEffect(() => {
    const timer = window.setTimeout(() => setSettled(value), ms)
    return () => window.clearTimeout(timer)
  }, [value, ms])
  return settled
}

/**
 * Everything lemondx does, as it happens, on every node: the live tail of each
 * node's recent events, merged by time. Kept by each `serve` in memory, so it
 * reaches back to that node's last restart; syslog is the record.
 */
export function LogsView({ nodes, groups, federated }: Props) {
  const [where, setWhere] = useState('all')
  const [level, setLevel] = useState<LogLevel | ''>('')
  const [kinds, setKinds] = useState<string[]>(['request', 'change', 'auth', 'system'])
  const [actor, setActor] = useState('')
  const [instance, setInstance] = useState('')
  const [search, setSearch] = useState('')
  const [req, setReq] = useState('')
  const [paused, setPaused] = useState(false)
  const [follow, setFollow] = useState(true)
  const settledSearch = useSettled(search)
  const settledActor = useSettled(actor)
  const settledInstance = useSettled(instance)

  const scope: TailScope = !federated ? { kind: 'node' }
    : where === 'all' ? { kind: 'cluster' }
      : where.startsWith('group:') ? { kind: 'cluster', group: where.slice(6) }
        : { kind: 'cluster', nodes: [where] }
  const filters: LogFilters = {
    level: level || undefined,
    kind: kinds.length === KINDS.length ? undefined : kinds.join(',') || 'none',
    actor: settledActor.trim() || undefined,
    instance: settledInstance.trim() || undefined,
    q: settledSearch.trim() || undefined,
    req: req || undefined,
  }
  const { events, notices, error, live, clear } = useLogTail({ scope, filters, paused })

  return (
    <>
      <div className="section-head">
        <h2>Logs</h2>
        <span className="faint" style={{ fontSize: 12.5 }}>
          what lemondx does and who asked, live{federated ? ' from every node' : ''}
        </span>
        <span className={`log-live${live ? ' is-live' : ''}`} style={{ marginLeft: 'auto' }}>
          {paused ? 'paused' : live ? 'live' : error ? 'reconnecting…' : 'connecting…'}
        </span>
      </div>

      <div className="card log-toolbar">
        {federated && (
          <select className="select" value={where} aria-label="Which nodes"
            onChange={(e) => setWhere(e.target.value)}>
            <option value="all">All nodes</option>
            {nodes.map((n) => <option key={n.name} value={n.name}>{n.name}{n.self ? ' (this node)' : ''}</option>)}
            {groups.map((g) => <option key={g.name} value={`group:${g.name}`}>Group: {g.name}</option>)}
          </select>
        )}
        <select className="select" value={level} aria-label="Lowest level"
          onChange={(e) => setLevel(e.target.value as LogLevel | '')}>
          <option value="">Any level</option>
          {LEVELS.map((l) => <option key={l} value={l}>{l} and above</option>)}
        </select>
        <div className="log-kinds" role="group" aria-label="Kinds">
          {KINDS.map((kind) => (
            <button type="button" key={kind}
              className={`btn btn-sm${kinds.includes(kind) ? ' is-on' : ''}`}
              aria-pressed={kinds.includes(kind)}
              onClick={() => setKinds(kinds.includes(kind) ? kinds.filter((k) => k !== kind)
                : [...kinds, kind])}>
              {kind}
            </button>
          ))}
        </div>
        <input className="input" value={actor} placeholder="who" aria-label="Actor"
          onChange={(e) => setActor(e.target.value)} />
        <input className="input" value={instance} placeholder="instance" aria-label="Instance"
          onChange={(e) => setInstance(e.target.value)} />
        <input className="input" value={search} placeholder="search…" aria-label="Search"
          onChange={(e) => setSearch(e.target.value)} />
        <div className="row-actions" style={{ marginLeft: 'auto' }}>
          <label className="check" title="Keep the newest event in view">
            <input type="checkbox" checked={follow} onChange={() => setFollow(!follow)} />
            <span>Follow</span>
          </label>
          <button type="button" className="btn btn-sm" onClick={() => setPaused(!paused)}>
            {paused ? <><PlayIcon size={13} /> Resume</> : <><PauseIcon size={13} /> Pause</>}
          </button>
          <button type="button" className="btn btn-sm btn-ghost" onClick={clear}
            title="Clear the list; new events keep coming">
            <TrashIcon size={13} /> Clear
          </button>
        </div>
      </div>

      {req && (
        <div className="log-chip-row">
          <span className="badge badge-info">
            request <span className="mono">{req}</span>
            <button type="button" className="log-chip-x" aria-label="Show every request"
              onClick={() => setReq('')}>×</button>
          </span>
          <span className="faint">everything this one request did, on every node it touched</span>
        </div>
      )}
      {error && <div className="banner banner-error"><div className="banner-body">{error}</div></div>}
      {notices.map((notice) => <div key={notice} className="hint log-notice">{notice}</div>)}

      <div className="card">
        <LogList events={events} showNode={federated} follow={follow && !paused}
          onRequest={setReq} onInstance={setInstance} onActor={setActor} />
      </div>
      <p className="hint" style={{ marginTop: 8 }}>
        Each node keeps its last 5000 events in memory, from its last restart. The full record is
        in syslog: <span className="mono">journalctl -t lemondx</span> on each host, or your remote
        syslog host. <span className="mono">lemondx logs -f</span> is the same tail in a terminal.
      </p>
    </>
  )
}
