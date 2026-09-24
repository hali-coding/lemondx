import { useCallback, useEffect, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { api } from '../lib/api'
import { bytes } from '../lib/format'
import type { ResourceInstance, Resources } from '../lib/types'
import { StatusBadge } from './StatusBadge'

const POLL_INTERVAL = 5000

/** CPU threads each instance used since the previous sample. */
type CpuUse = Record<string, number>

/** What the host has, and how much of it instances have claimed. */
export function ResourcesView() {
  const [data, setData] = useState<Resources | null>(null)
  const [cpuUse, setCpuUse] = useState<CpuUse | null>(null)
  const [error, setError] = useState<string | null>(null)
  // The daemon only reports a cumulative CPU-time counter, so usage is the
  // difference between two polls. The first poll has nothing to compare with.
  const previous = useRef<{ at: number; ns: Record<string, number> } | null>(null)

  const load = useCallback((signal?: AbortSignal) => {
    api.resources(signal)
      .then((next) => {
        const at = performance.now()
        const ns: Record<string, number> = {}
        next.instances.forEach((i) => { ns[i.name] = i.cpu_time_ns })
        const last = previous.current
        if (last && at > last.at) {
          const use: CpuUse = {}
          for (const instance of next.instances) {
            const before = last.ns[instance.name]
            // A restart resets the counter; count that interval as unknown.
            if (!instance.active || before === undefined || instance.cpu_time_ns < before) continue
            use[instance.name] = (instance.cpu_time_ns - before) / ((at - last.at) * 1e6)
          }
          setCpuUse(use)
        }
        previous.current = { at, ns }
        setData(next)
        setError(null)
      })
      .catch((cause) => {
        if ((cause as Error).name !== 'AbortError') setError((cause as Error).message)
      })
  }, [])

  useEffect(() => {
    const controller = new AbortController()
    load(controller.signal)
    const timer = window.setInterval(() => load(controller.signal), POLL_INTERVAL)
    return () => { controller.abort(); window.clearInterval(timer) }
  }, [load])

  if (error && !data) {
    return (
      <div className="banner banner-error">
        <div className="banner-body"><p style={{ margin: 0 }}>{error}</p></div>
      </div>
    )
  }
  if (!data) {
    return (
      <div className="card">
        <div className="loading-wrap"><span className="spinner" /> Reading host resources…</div>
      </div>
    )
  }

  const { host, cpu, memory, storage, instances } = data
  const cpuUsed = cpuUse ? Object.values(cpuUse).reduce((a, b) => a + b, 0) : null

  return (
    <>
      <section className="card res-host">
        <HostFact label="Processor" value={host.cpu_model || host.architecture || '—'} />
        <HostFact label="Threads"
          value={`${host.cpu_threads}${host.cpu_cores ? ` on ${host.cpu_cores} cores` : ''}`} />
        <HostFact label="Memory" value={bytes(host.memory_total)} />
        <HostFact label="Architecture" value={host.architecture || '—'} />
      </section>

      <div className="res-grid">
        <ResourceCard title="CPU" figure={`${cpu.allocated} of ${cpu.total}`} unit="threads allocated">
          <Meter
            label="Allocated"
            total={cpu.total}
            segments={[
              { value: cpu.allocated, title: `${cpu.allocated} threads held by running instances` },
              { value: cpu.stopped, soft: true,
                title: `${cpu.stopped} more if every stopped instance started` },
            ]}
            overcommit
            detail={cpu.stopped ? `+${cpu.stopped} stopped` : undefined}
            format={(n) => String(n)}
          />
          <Meter
            label="Used by instances"
            total={cpu.total}
            segments={cpuUsed === null ? [] : [
              { value: cpuUsed, title: `${threads(cpuUsed)} threads busy, averaged over the last poll` },
            ]}
            format={threads}
            pending={cpuUsed === null ? 'measuring…' : undefined}
          />
          <Unlimited names={cpu.unlimited} what="CPU">
            can be scheduled on all {cpu.total} threads.
          </Unlimited>
        </ResourceCard>

        <ResourceCard title="Memory" figure={`${bytes(memory.allocated)} of ${bytes(memory.total)}`}
          unit="allocated">
          <Meter
            label="Allocated"
            total={memory.total}
            segments={[
              { value: memory.allocated, title: `${bytes(memory.allocated)} held by running instances` },
              { value: memory.stopped, soft: true,
                title: `${bytes(memory.stopped)} more if every stopped instance started` },
            ]}
            overcommit
            detail={memory.stopped ? `+${bytes(memory.stopped)} stopped` : undefined}
            format={bytes}
          />
          <Meter
            label="In use on the host"
            total={memory.total}
            segments={[
              { value: memory.instances_used, title: `${bytes(memory.instances_used)} used by instances` },
              { value: Math.max(0, memory.used - memory.instances_used), soft: true,
                title: `${bytes(Math.max(0, memory.used - memory.instances_used))} used by everything else on the host` },
            ]}
            legend={['instances', 'rest of host']}
            format={bytes}
          />
          <Unlimited names={memory.unlimited} what="memory">
            can use all the host's free memory.
          </Unlimited>
        </ResourceCard>

        {storage.map((pool) => (
          <ResourceCard key={pool.name} title={`Disk · ${pool.name}`}
            badge={<span className="badge badge-dim">{pool.driver}</span>}
            figure={`${bytes(pool.allocated)} of ${bytes(pool.total)}`} unit="allocated">
            <Meter
              label="Allocated"
              total={pool.total}
              segments={[{ value: pool.allocated, title: `${bytes(pool.allocated)} of root disk sizes` }]}
              overcommit
              format={bytes}
            />
            <Meter
              label="Used"
              total={pool.total}
              segments={[{ value: pool.used,
                title: `${bytes(pool.used)} used, including images and snapshots` }]}
              format={bytes}
            />
            {!pool.supports_quota && (
              <p className="res-note">
                The {pool.driver} driver does not enforce disk sizes, so these are
                intentions rather than limits.
              </p>
            )}
            <Unlimited names={pool.unlimited} what="disk size">
              can grow until the pool is full.
            </Unlimited>
          </ResourceCard>
        ))}
      </div>

      <div className="section-head">
        <h2>Per instance</h2>
        <span className="faint" style={{ fontSize: 12.5 }}>
          limits as configured; faint values are the daemon's defaults for VMs
        </span>
      </div>
      {instances.length === 0 ? (
        <div className="card">
          <div className="empty">
            <h3>No instances</h3>
            <p>Nothing has claimed any resources yet.</p>
          </div>
        </div>
      ) : (
        <div className="card table-scroll">
          <table className="ctable res-table">
            <thead>
              <tr>
                <th>Name</th>
                <th className="optional">State</th>
                <th>CPU</th>
                <th>Memory</th>
                <th className="optional">Disk</th>
              </tr>
            </thead>
            <tbody>
              {instances.map((instance) => (
                <InstanceRow key={instance.name} instance={instance} cpuUse={cpuUse?.[instance.name]} />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  )
}

function InstanceRow({ instance, cpuUse }: { instance: ResourceInstance; cpuUse?: number }) {
  const { cpu, memory, disk } = instance
  const pinned = cpu.count !== null && !/^\d+$/.test(cpu.limit)
  return (
    <tr>
      <td>
        <span className="mono">{instance.name}</span>
        {instance.type === 'virtual-machine' && <span className="badge badge-info res-vm">VM</span>}
      </td>
      <td className="optional"><StatusBadge status={instance.status} /></td>
      <td className="num">
        <Limit implicit={cpu.implicit}
          value={cpu.count === null ? (cpu.limit || null) : `${cpu.count}${pinned ? ` (pinned ${cpu.limit})` : ''}`} />
        {instance.active && cpuUse !== undefined && (
          <div className="res-sub">{threads(cpuUse)} busy</div>
        )}
      </td>
      <td className="num">
        <Limit implicit={memory.implicit}
          value={memory.bytes === null ? (memory.limit || null) : bytes(memory.bytes)} />
        {instance.active && memory.usage > 0 && (
          <div className="res-sub">{bytes(memory.usage)} in use</div>
        )}
      </td>
      <td className="optional num">
        <Limit implicit={disk.implicit}
          value={disk.bytes === null ? (disk.size || null) : bytes(disk.bytes)} />
        <div className="res-sub">
          {disk.usage > 0 ? `${bytes(disk.usage)} used on ` : 'on '}
          {disk.pool ?? 'no pool'}
        </div>
      </td>
    </tr>
  )
}

function Limit({ value, implicit }: { value: string | null; implicit: boolean }) {
  if (value === null) return <span className="faint">unlimited</span>
  if (implicit) return <span className="faint" title="Not set; this is the daemon's default">{value} default</span>
  return <span>{value}</span>
}

function HostFact({ label, value }: { label: string; value: string }) {
  return (
    <div className="res-host-fact">
      <div className="stat-label">{label}</div>
      <div className="res-host-value">{value}</div>
    </div>
  )
}

function ResourceCard({ title, badge, figure, unit, children }: {
  title: string; badge?: ReactNode; figure: string; unit: string; children: ReactNode
}) {
  return (
    <section className="card res-card">
      <header className="res-card-head">
        <h3>{title}</h3>
        {badge}
      </header>
      <div className="res-figure">
        {figure} <span className="faint">{unit}</span>
      </div>
      {children}
    </section>
  )
}

interface Segment {
  value: number
  title: string
  /** A lighter step of the same hue: potential, or someone else's share. */
  soft?: boolean
}

/**
 * A capacity bar. Allocations can exceed the host, so the bar then widens its
 * scale to the total claimed and marks where the host runs out.
 */
function Meter({ label, total, segments, format, overcommit, detail, legend, pending }: {
  label: string
  total: number
  segments: Segment[]
  format: (n: number) => string
  /** This bar may legitimately pass 100% (limits are promises, not reservations). */
  overcommit?: boolean
  detail?: string
  legend?: [string, string]
  pending?: string
}) {
  const shown = segments.filter((s) => s.value > 0)
  const claimed = shown.reduce((sum, s) => sum + s.value, 0)
  // An allocation bar reports what is held now and shows the rest as
  // potential; a usage bar reports everything in it.
  const headline = overcommit ? (segments[0]?.value ?? 0) : claimed
  const ratio = total > 0 ? headline / total : 0
  const scale = Math.max(total, claimed) || 1

  // Severity always comes with words, never with color alone.
  let tone = ''
  let flag: ReactNode = null
  if (overcommit && ratio > 1) {
    tone = 'res-meter-warn'
    flag = <span className="badge badge-warn">overcommitted</span>
  } else if (!overcommit && ratio >= 0.9) {
    tone = 'res-meter-danger'
    flag = <span className="badge badge-danger">nearly full</span>
  }

  return (
    <div className={`res-meter ${tone}`}>
      <div className="res-meter-label">
        <span className="dim">{label}</span>
        {flag}
        <span className="res-meter-value num">
          {pending ? <span className="faint">{pending}</span> : (
            <>
              {headline > 0 ? format(headline) : '0'}
              <span className="faint"> · {Math.round(ratio * 100)}%</span>
              {detail && <span className="faint"> · {detail}</span>}
            </>
          )}
        </span>
      </div>
      <div className="res-track" role="img"
        aria-label={`${label}: ${pending ?? `${headline > 0 ? format(headline) : '0'} of ${format(total)}`}`}>
        {shown.map((segment, index) => (
          <div
            key={index}
            className={`res-fill${segment.soft ? ' res-fill-soft' : ''}`}
            style={{ width: `${(segment.value / scale) * 100}%` }}
            title={segment.title}
          />
        ))}
        {claimed > total && total > 0 && (
          <div className="res-capacity" style={{ left: `${(total / scale) * 100}%` }}
            title={`Host capacity: ${format(total)}`} />
        )}
      </div>
      {legend && !pending && (
        <div className="res-legend">
          <span><i className="res-swatch" />{legend[0]}</span>
          <span><i className="res-swatch res-fill-soft" />{legend[1]}</span>
        </div>
      )}
    </div>
  )
}

function Unlimited({ names, what, children }: { names: string[]; what: string; children: ReactNode }) {
  if (names.length === 0) return null
  return (
    <p className="res-note">
      <span className="mono">{names.join(', ')}</span>{' '}
      {names.length === 1 ? 'has' : 'have'} no {what} limit and {children}
    </p>
  )
}

function threads(n: number): string {
  if (n > 0 && n < 0.01) return '<0.01'
  return n.toFixed(n < 10 ? 2 : 1)
}
