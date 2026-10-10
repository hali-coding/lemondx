import { Fragment, memo, useMemo, useState } from 'react'
import type { ReactNode } from 'react'
import { useMetrics } from '../hooks/useMetrics'
import type { NodeHistory } from '../hooks/useMetrics'
import type { ClusterNode, InstanceMetrics, MetricPoint, NodeGroup } from '../lib/types'
import { LineChart, Sparkline } from './LineChart'
import type { Series } from './LineChart'
import { PauseIcon, PlayIcon } from './Icons'

interface Props {
  nodes: ClusterNode[]
  groups: NodeGroup[]
  federated: boolean
}

const WINDOWS = [
  { seconds: 300, label: 'Last 5 min' },
  { seconds: 900, label: 'Last 15 min' },
  { seconds: 1800, label: 'Last 30 min' },
]
const RATES = [2, 5, 10, 30]
const SERIES_SLOTS = 8
const SETTINGS_KEY = 'lemondx-monitor'
const SPARK_SECONDS = 300

type SortKey = 'name' | 'node' | 'cpu' | 'memory' | 'rx' | 'tx'

const percent = (v: number) => `${v >= 10 || v === 0 ? Math.round(v) : v.toFixed(1)}%`

/** Binary units, no more than three digits before the unit. */
function size(v: number) {
  const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB']
  let n = Math.abs(v)
  let unit = 0
  while (n >= 1024 && unit < units.length - 1) { n /= 1024; unit += 1 }
  const digits = unit === 0 || n >= 100 || Number.isInteger(n) ? 0 : 1
  return `${n.toFixed(digits)} ${units[unit]}`
}
const rate = (v: number) => `${size(v)}/s`

function latest(points: MetricPoint[], field: 1 | 2 | 3 | 4): number | null {
  for (let i = points.length - 1; i >= 0; i -= 1) {
    const v = points[i][field]
    if (v !== null) return v
  }
  return null
}

function column(points: MetricPoint[], field: 1 | 2 | 3 | 4, scale = 1): [number, number | null][] {
  return points.map((p) => [p[0], p[field] === null ? null : (p[field] as number) * scale])
}

/** The range and refresh rate this browser last used: how one person likes to
 *  look, so kept here rather than on the server. */
function savedSettings(): { range: number; every: number } {
  const fallback = { range: 900, every: 2 }
  try {
    const saved = JSON.parse(window.localStorage.getItem(SETTINGS_KEY) ?? '{}')
    return {
      range: WINDOWS.some((w) => w.seconds === saved.range) ? saved.range : fallback.range,
      every: RATES.includes(saved.every) ? saved.every : fallback.every,
    }
  } catch {
    return fallback
  }
}

function saveSettings(settings: { range: number; every: number }) {
  try {
    window.localStorage.setItem(SETTINGS_KEY, JSON.stringify(settings))
  } catch { /* private mode: this visit only */ }
}

/**
 * CPU, memory and network over time on every node and in every instance: the
 * page to keep open during a load test. Each `serve` samples itself every
 * couple of seconds and keeps half an hour, so the page opens with history
 * rather than starting from the moment it was opened.
 */
export function MonitorView({ nodes, groups, federated }: Props) {
  const [where, setWhere] = useState('all')
  const [settings, setSettings] = useState(savedSettings)
  const { range, every } = settings
  const [paused, setPaused] = useState(false)
  // A node picked from the legend or the table: shown alone, with its
  // instances charted. Narrowed here rather than by asking again, since the
  // page already holds its history.
  const [focus, setFocus] = useState<string | null>(null)

  const scope = !federated || where === 'all' ? {}
    : where.startsWith('group:') ? { groups: [where.slice(6)] } : { nodes: [where] }
  const { history, error, live } = useMetrics({ window: range, paused, every, ...scope })
  const focused = history.find((h) => h.node === focus)
  const shown = focused ? [focused] : history

  function change(next: Partial<typeof settings>) {
    const merged = { ...settings, ...next }
    setSettings(merged)
    saveSettings(merged)
  }

  // A node keeps its colour whatever is in view: the slot follows the node's
  // place in the whole cluster, so narrowing to a group repaints nobody.
  const colorOf = useMemo(() => {
    const order = nodes.length ? nodes.map((n) => n.name) : history.map((h) => h.node)
    return (node: string) => {
      const slot = order.indexOf(node)
      return slot >= 0 && slot < SERIES_SLOTS ? `var(--series-${slot + 1})` : 'var(--text-faint)'
    }
  }, [nodes, history])

  // The newest sample anywhere is "now": every node is on its own clock.
  const end = Math.max(0, ...history.map((h) => h.at ?? 0))
  const start = end - range
  const gap = Math.max(...history.map((h) => h.period), 2) * 3

  return (
    <>
      <div className="section-head">
        <h2>Monitor</h2>
        <span className="faint" style={{ fontSize: 12.5 }}>
          CPU, memory and network{federated ? ' on every node' : ''}, sampled every few seconds
        </span>
        <span className={`log-live${live && !paused ? ' is-live' : ''}`} style={{ marginLeft: 'auto' }}>
          {paused ? 'paused' : live ? 'live' : error ? 'reconnecting…' : 'connecting…'}
        </span>
      </div>

      <div className="card log-toolbar">
        {federated && (
          <select className="select" value={where} aria-label="Which nodes"
            onChange={(e) => { setWhere(e.target.value); setFocus(null) }}>
            <option value="all">All nodes</option>
            {nodes.map((n) => <option key={n.name} value={n.name}>{n.name}{n.self ? ' (this node)' : ''}</option>)}
            {groups.map((g) => <option key={g.name} value={`group:${g.name}`}>Group: {g.name}</option>)}
          </select>
        )}
        <select className="select" value={range} aria-label="Time range"
          onChange={(e) => change({ range: Number(e.target.value) })}>
          {WINDOWS.map((w) => <option key={w.seconds} value={w.seconds}>{w.label}</option>)}
        </select>
        <select className="select" value={every} aria-label="Refresh every"
          title="How often this page asks for new samples; each node takes one every 2 seconds"
          onChange={(e) => change({ every: Number(e.target.value) })}>
          {RATES.map((r) => <option key={r} value={r}>Refresh every {r}s</option>)}
        </select>
        <button type="button" className="btn btn-sm" onClick={() => setPaused(!paused)}
          title={paused ? 'Carry on, catching up on what was sampled meanwhile'
            : 'Hold the charts still to look at them'}>
          {paused ? <PlayIcon size={13} /> : <PauseIcon size={13} />}
          {paused ? 'Resume' : 'Pause'}
        </button>
      </div>

      {error && !history.length && <div className="banner banner-error">{error}</div>}
      {history.filter((h) => h.error).map((h) => (
        <div key={h.node} className="banner banner-error monitor-node-error">
          <strong>{h.node}</strong>: {h.error}
          {h.host.length ? ' — showing what it last reported.' : ''}
        </div>
      ))}

      {focused && (
        <div className="monitor-focus">
          <button type="button" className="btn btn-sm btn-ghost" onClick={() => setFocus(null)}>
            ← All nodes
          </button>
          <span className="chart-key" style={{ background: colorOf(focused.node) }} />
          <strong>{focused.node}</strong>
          {focused.self && <span className="faint">this node</span>}
          <span className="faint">
            {focused.cpuThreads} threads · {size(focused.memoryTotal)}
            {focused.uplink ? ` · uplink ${focused.uplink}` : ''}
          </span>
        </div>
      )}
      {history.length > 0 && (
        <NodeCharts history={shown} start={start} end={end} gap={gap} colorOf={colorOf}
          onFocus={setFocus} />
      )}
      {shown.length === 1 && (
        <InstanceCharts node={shown[0]} start={start} end={end} gap={gap} />
      )}
      {history.length > 0 && !focused && (
        <NodeTable history={history} colorOf={colorOf} federated={federated} onFocus={setFocus} />
      )}
      {history.length > 0 && (
        <InstanceTable history={shown} start={start} end={end} gap={gap} colorOf={colorOf}
          federated={federated && !focused} />
      )}
      {!history.length && !error && <div className="card empty"><p>Gathering the first samples…</p></div>}
    </>
  )
}

/** Each node's latest host figures. */
function nodeNow(h: NodeHistory) {
  const memory = latest(h.host, 2)
  return {
    cpu: latest(h.host, 1),
    memory, memoryShare: memory !== null && h.memoryTotal ? (memory / h.memoryTotal) * 100 : null,
    rx: latest(h.host, 3), tx: latest(h.host, 4),
  }
}

function sum(values: (number | null)[]) {
  const known = values.filter((v): v is number => v !== null)
  return known.length ? known.reduce((a, b) => a + b, 0) : null
}

const NodeCharts = ({ history, start, end, gap, colorOf, onFocus }: {
  history: NodeHistory[]
  start: number
  end: number
  gap: number
  colorOf: (node: string) => string
  onFocus: (node: string) => void
}) => {
  // One crosshair for all four, so a moment in a test reads across every
  // measure at once. Held here, not by the page: a pointer move must not
  // redraw the instance table.
  const [hoverAt, onHover] = useState<number | null>(null)
  const series = (pick: (h: NodeHistory) => [number, number | null][]): Series[] =>
    history.map((h) => ({ key: h.node, label: h.node, color: colorOf(h.node), points: pick(h) }))
  const now = history.map(nodeNow)
  const threads = history.reduce((a, h) => a + h.cpuThreads, 0)
  const memoryTotal = history.reduce((a, h) => a + h.memoryTotal, 0)
  // Cluster-wide figures: CPU weighted by each node's threads, so a big host
  // idling is not outvoted by a small one at full load.
  const cpu = threads ? sum(now.map((n, i) => n.cpu === null ? null
    : n.cpu * history[i].cpuThreads))! / threads : null
  const used = sum(now.map((n) => n.memory))
  const several = history.length > 1
  const common = { start, end, gap, hoverAt, onHover }

  const cards: { title: string; headline: string; chart: ReactNode }[] = [
    { title: 'CPU', headline: cpu === null ? '—' : percent(cpu),
      chart: <LineChart {...common} label="Host CPU by node" max={100} format={percent}
        series={series((h) => column(h.host, 1))} /> },
    { title: 'Memory', headline: used === null || !memoryTotal ? '—'
        : `${size(used)} of ${size(memoryTotal)}`,
      chart: <LineChart {...common} label="Host memory used by node" max={100} format={percent}
        series={series((h) => h.memoryTotal ? column(h.host, 2, 100 / h.memoryTotal) : [])} /> },
    { title: 'Network in', headline: rate(sum(now.map((n) => n.rx)) ?? 0),
      chart: <LineChart {...common} label="Bytes received per second by node" binary format={rate}
        series={series((h) => column(h.host, 3))} /> },
    { title: 'Network out', headline: rate(sum(now.map((n) => n.tx)) ?? 0),
      chart: <LineChart {...common} label="Bytes sent per second by node" binary format={rate}
        series={series((h) => column(h.host, 4))} /> },
  ]

  return (
    <section className="monitor-section">
      {several && (
        <div className="chart-legend" aria-label="Nodes">
          {history.map((h) => (
            <button key={h.node} type="button" className="chart-legend-item chart-legend-link"
              title={`Show ${h.node} alone, with its instances`} onClick={() => onFocus(h.node)}>
              <span className="chart-key" style={{ background: colorOf(h.node) }} />{h.node}
            </button>
          ))}
        </div>
      )}
      <div className="monitor-grid">
        {cards.map((c) => (
          <div key={c.title} className="card monitor-card">
            <div className="monitor-card-head">
              <span className="monitor-card-title">{c.title}</span>
              <span className="monitor-card-value">{c.headline}</span>
              {several && <span className="faint monitor-card-note">all nodes</span>}
            </div>
            {c.chart}
          </div>
        ))}
      </div>
    </section>
  )
}

const NodeTable = memo(function NodeTable({ history, colorOf, federated, onFocus }: {
  history: NodeHistory[]
  colorOf: (node: string) => string
  federated: boolean
  onFocus: (node: string) => void
}) {
  return (
    <div className="card table-scroll monitor-section">
      <table className="ctable monitor-table">
        <thead>
          <tr>
            <th>{federated ? 'Node' : 'Host'}</th>
            <th>Running</th>
            <th className="num">CPU</th>
            <th className="num">Memory</th>
            <th className="num">In</th>
            <th className="num">Out</th>
          </tr>
        </thead>
        <tbody>
          {history.map((h) => {
            const now = nodeNow(h)
            const running = h.instances.filter((i) => i.status === 'Running').length
            return (
              <tr key={h.node} className={federated ? undefined : 'row-static'}
                onClick={federated ? () => onFocus(h.node) : undefined}
                title={federated ? `Show ${h.node} alone, with its instances` : undefined}>
                <td>
                  <span className="cname">
                    {federated && <span className="chart-key" style={{ background: colorOf(h.node) }} />}
                    {h.node}
                    {h.self && federated && <span className="faint" style={{ fontWeight: 400 }}>this node</span>}
                    {h.error && <span className="badge badge-danger">unreachable</span>}
                  </span>
                </td>
                <td>{running} of {h.instances.length}</td>
                <td className="num">
                  {now.cpu === null ? '—' : percent(now.cpu)}
                  <span className="faint"> of {h.cpuThreads} threads</span>
                </td>
                <td className="num">
                  {now.memory === null ? '—' : size(now.memory)}
                  <span className="faint"> / {size(h.memoryTotal)}</span>
                </td>
                <td className="num">{now.rx === null ? '—' : rate(now.rx)}</td>
                <td className="num">
                  {now.tx === null ? '—' : rate(now.tx)}
                  {h.uplink && <span className="faint"> {h.uplink}</span>}
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
})

interface Row {
  key: string
  node: string
  instance: InstanceMetrics
  cpu: number | null
  memory: number | null
  rx: number | null
  tx: number | null
}

const InstanceTable = ({ history, start, end, gap, colorOf, federated }: {
  history: NodeHistory[]
  start: number
  end: number
  gap: number
  colorOf: (node: string) => string
  federated: boolean
}) => {
  const [sort, setSort] = useState<{ key: SortKey; down: boolean }>({ key: 'cpu', down: true })
  const [filter, setFilter] = useState('')
  const [stopped, setStopped] = useState(false)
  const [open, setOpen] = useState<Set<string>>(new Set())

  const rows: Row[] = history.flatMap((h) => h.instances.map((i) => {
    const running = i.status === 'Running'
    return {
      key: `${h.node}/${i.name}`, node: h.node, instance: i,
      cpu: running ? latest(i.points, 1) : null, memory: running ? latest(i.points, 2) : null,
      rx: running ? latest(i.points, 3) : null, tx: running ? latest(i.points, 4) : null,
    }
  }))
  const needle = filter.trim().toLowerCase()
  const shown = rows
    .filter((r) => stopped || r.instance.status === 'Running')
    .filter((r) => !needle || [r.instance.name, r.node, r.instance.template, r.instance.stack]
      .some((v) => v && v.toLowerCase().includes(needle)))
    .sort((a, b) => {
      const { key, down } = sort
      const order = key === 'name' ? a.instance.name.localeCompare(b.instance.name)
        : key === 'node' ? a.node.localeCompare(b.node) || a.instance.name.localeCompare(b.instance.name)
          : (a[key] ?? -1) - (b[key] ?? -1)
      return down ? -order : order
    })
  const hidden = rows.length - rows.filter((r) => r.instance.status === 'Running').length

  function header(key: SortKey, label: string, numeric = false) {
    const active = sort.key === key
    return (
      <th className={numeric ? 'num' : undefined}
        aria-sort={active ? (sort.down ? 'descending' : 'ascending') : 'none'}>
        <button type="button" className="th-sort"
          onClick={() => setSort({ key, down: active ? !sort.down : numeric })}>
          {label}{active ? (sort.down ? ' ↓' : ' ↑') : ''}
        </button>
      </th>
    )
  }

  function toggle(key: string) {
    setOpen((current) => {
      const next = new Set(current)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  }

  const columns = federated ? 8 : 7
  // A cell's trend is the last few minutes whatever the range: at half an
  // hour, a minute-long spike would be a pixel wide.
  const sparkStart = Math.max(start, end - SPARK_SECONDS)
  return (
    <section className="monitor-section">
      <div className="monitor-table-head">
        <h3>Instances</h3>
        <input className="input" placeholder="Filter by name, node, template or stack"
          value={filter} onChange={(e) => setFilter(e.target.value)} aria-label="Filter instances" />
        {hidden > 0 && (
          <label className="faint monitor-check">
            <input type="checkbox" checked={stopped} onChange={(e) => setStopped(e.target.checked)} />
            Show {hidden} not running
          </label>
        )}
      </div>
      <div className="card table-scroll">
        <table className="ctable monitor-table">
          <thead>
            <tr>
              {header('name', 'Instance')}
              {federated && header('node', 'Node')}
              <th>From</th>
              {header('cpu', 'CPU', true)}
              {header('memory', 'Memory', true)}
              {header('rx', 'In', true)}
              {header('tx', 'Out', true)}
              <th className="num">Procs</th>
            </tr>
          </thead>
          <tbody>
            {shown.map((r) => {
              const running = r.instance.status === 'Running'
              const expanded = open.has(r.key)
              return (
                <Fragment key={r.key}>
                  <tr aria-expanded={expanded} onClick={() => toggle(r.key)}
                    className={running ? undefined : 'monitor-row-stopped'}>
                    <td>
                      <span className="cname">
                        <span className={`monitor-caret${expanded ? ' is-open' : ''}`} aria-hidden>▸</span>
                        {r.instance.name}
                        {r.instance.type === 'virtual-machine' && <span className="vm-tag">VM</span>}
                        {!running && <span className="badge badge-dim">{r.instance.status.toLowerCase()}</span>}
                      </span>
                    </td>
                    {federated && (
                      <td>
                        <span className="monitor-node">
                          <span className="chart-key" style={{ background: colorOf(r.node) }} />{r.node}
                        </span>
                      </td>
                    )}
                    <td className="faint">
                      {[r.instance.stack && `stack ${r.instance.stack}`, r.instance.template]
                        .filter(Boolean).join(' · ') || '—'}
                    </td>
                    <td className="num">
                      <span className="monitor-cell">
                        <Sparkline points={column(r.instance.points, 1)} start={sparkStart} end={end}
                          gap={gap} floor={10} />
                        <span>{r.cpu === null ? '—' : percent(r.cpu)}</span>
                      </span>
                    </td>
                    <td className="num">
                      <span className="monitor-cell">
                        <Sparkline points={column(r.instance.points, 2)} start={sparkStart} end={end}
                          gap={gap} color="var(--series-3)" />
                        <span>{r.memory === null ? '—' : size(r.memory)}</span>
                      </span>
                    </td>
                    <td className="num">{r.rx === null ? '—' : rate(r.rx)}</td>
                    <td className="num">{r.tx === null ? '—' : rate(r.tx)}</td>
                    <td className="num">{running ? r.instance.processes : '—'}</td>
                  </tr>
                  {expanded && (
                    <tr className="monitor-detail">
                      <td colSpan={columns}>
                        <InstanceDetail instance={r.instance} start={start} end={end} gap={gap} />
                      </td>
                    </tr>
                  )}
                </Fragment>
              )
            })}
            {!shown.length && (
              <tr className="row-static">
                <td colSpan={columns} className="faint">
                  {rows.length ? 'Nothing matches.' : 'No instances on these nodes.'}
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </section>
  )
}

function InstanceDetail({ instance, start, end, gap }: {
  instance: InstanceMetrics
  start: number
  end: number
  gap: number
}) {
  const one = (label: string, points: [number, number | null][], color: string): Series[] =>
    [{ key: label, label, color, points }]
  return (
    <div className="monitor-grid monitor-grid-3">
      <div>
        <div className="monitor-card-title">CPU <span className="faint">(100% is one core)</span></div>
        <LineChart start={start} end={end} gap={gap} height={130} format={percent}
          label={`${instance.name} CPU`} series={one('CPU', column(instance.points, 1), 'var(--series-1)')} />
      </div>
      <div>
        <div className="monitor-card-title">Memory</div>
        <LineChart start={start} end={end} gap={gap} height={130} binary format={size}
          label={`${instance.name} memory`}
          series={one('Memory', column(instance.points, 2), 'var(--series-3)')} />
      </div>
      <div>
        <div className="monitor-card-title">
          Network <span className="chart-legend-inline">
            <span className="chart-key" style={{ background: 'var(--series-1)' }} />in
            <span className="chart-key" style={{ background: 'var(--series-2)' }} />out
          </span>
        </div>
        <LineChart start={start} end={end} gap={gap} height={130} binary format={rate}
          label={`${instance.name} network`} series={[
            { key: 'in', label: 'in', color: 'var(--series-1)', points: column(instance.points, 3) },
            { key: 'out', label: 'out', color: 'var(--series-2)', points: column(instance.points, 4) },
          ]} />
      </div>
    </div>
  )
}

const INSTANCE_LINES = SERIES_SLOTS

/**
 * A colour slot per key that stays put while the key is in view: a newcomer
 * takes a free slot and one that leaves frees its own, so which instances are
 * the busiest can change without repainting the ones that stay.
 */
function useSlots(keys: string[]) {
  const [held, setHeld] = useState(() => new Map<string, number>())
  const next = new Map([...held].filter(([key]) => keys.includes(key)))
  for (const key of keys) {
    if (next.has(key)) continue
    const taken = new Set(next.values())
    const free = Array.from({ length: SERIES_SLOTS }, (_, i) => i).find((i) => !taken.has(i))
    if (free !== undefined) next.set(key, free)
  }
  const changed = next.size !== held.size || [...next].some(([key, slot]) => held.get(key) !== slot)
  // Adjusted while rendering, so the first frame with a newcomer already has its colour.
  if (changed) setHeld(next)
  return (key: string) => {
    const slot = next.get(key)
    return slot === undefined ? 'var(--text-faint)' : `var(--series-${slot + 1})`
  }
}

function mean(points: MetricPoint[], field: 1 | 2 | 3 | 4, from: number) {
  let total = 0
  let count = 0
  for (const p of points) {
    const v = p[field]
    if (p[0] >= from && v !== null) { total += v; count += 1 }
  }
  return count ? total / count : 0
}

/**
 * One node's instances side by side: what on the host is using it. A line
 * each for the busiest few by CPU over the range, since past eight lines a
 * chart is a tangle; the instance table below still lists every one.
 */
function InstanceCharts({ node, start, end, gap }: {
  node: NodeHistory
  start: number
  end: number
  gap: number
}) {
  const [hoverAt, onHover] = useState<number | null>(null)
  const running = node.instances.filter((i) => i.status === 'Running')
  const busiest = [...running]
    .map((i) => ({ i, load: mean(i.points, 1, start) }))
    .sort((a, b) => b.load - a.load || a.i.name.localeCompare(b.i.name))
    .slice(0, INSTANCE_LINES)
    .map((x) => x.i)
    .sort((a, b) => a.name.localeCompare(b.name))
  const colorOf = useSlots(busiest.map((i) => i.name))
  if (!busiest.length) return null

  const series = (field: 1 | 2 | 3 | 4): Series[] => busiest.map((i) => ({
    key: i.name, label: i.name, color: colorOf(i.name), points: column(i.points, field),
  }))
  const common = { start, end, gap, hoverAt, onHover }
  const cards: { title: string; chart: ReactNode }[] = [
    { title: 'CPU', chart: <LineChart {...common} label="CPU by instance" format={percent}
        series={series(1)} /> },
    { title: 'Memory', chart: <LineChart {...common} label="Memory by instance" binary format={size}
        series={series(2)} /> },
    { title: 'Network in', chart: <LineChart {...common} label="Bytes received per second by instance"
        binary format={rate} series={series(3)} /> },
    { title: 'Network out', chart: <LineChart {...common} label="Bytes sent per second by instance"
        binary format={rate} series={series(4)} /> },
  ]
  return (
    <section className="monitor-section">
      <div className="monitor-table-head">
        <h3>By instance</h3>
        <span className="faint" style={{ fontSize: 12.5 }}>
          {running.length > busiest.length
            ? `the ${busiest.length} busiest of ${running.length} running, by CPU over the range`
            : '100% CPU is one core'}
        </span>
      </div>
      {busiest.length > 1 && (
        <div className="chart-legend" aria-label="Instances">
          {busiest.map((i) => (
            <span key={i.name} className="chart-legend-item">
              <span className="chart-key" style={{ background: colorOf(i.name) }} />{i.name}
            </span>
          ))}
        </div>
      )}
      <div className="monitor-grid">
        {cards.map((c) => (
          <div key={c.title} className="card monitor-card">
            <div className="monitor-card-head">
              <span className="monitor-card-title">{c.title}</span>
            </div>
            {c.chart}
          </div>
        ))}
      </div>
    </section>
  )
}
