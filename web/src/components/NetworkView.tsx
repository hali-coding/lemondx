import { useCallback, useEffect, useRef, useState } from 'react'
import { useCanWrite } from '../hooks/useAuth'
import type { FormEvent, ReactNode } from 'react'
import { api, calls } from '../lib/api'
import type { ApiCall } from '../lib/api'
import { bytes } from '../lib/format'
import { displayAddress, subnetStatus } from '../lib/cidr'
import type {
  FabricChangeResult, FabricCheck, FabricDeleteResult, FabricMember, FabricOverview,
  FabricSummary, NetworkDetail, NetworkFamily, NetworkRequest, NetworkSummary, SubnetInUse,
} from '../lib/types'
import type { ToastKind } from '../hooks/useToasts'
import { Modal } from './Modal'
import { PlusIcon } from './Icons'
import { SubnetField } from './SubnetField'

interface Props {
  onNotify: (kind: ToastKind, title: string, detail?: string) => void
}

/** How the daemon's networking is wired up, and the bridges lemondx manages. */
export function NetworkView({ onNotify }: Props) {
  const canWrite = useCanWrite()
  const [summaries, setSummaries] = useState<NetworkSummary[] | null>(null)
  const [details, setDetails] = useState<Record<string, NetworkDetail>>({})
  const [error, setError] = useState<string | null>(null)
  const [dialog, setDialog] = useState<NetworkDetail | 'new' | null>(null)
  const [pendingDelete, setPendingDelete] = useState<NetworkDetail | null>(null)
  const [busy, setBusy] = useState(false)
  // Pauses the 5s poll while a change is in flight. That stops new polls
  // only; loadSequence is what keeps one already under way from landing after
  // the refresh that follows the change and showing the old settings.
  const mutating = useRef(false)
  const loadSequence = useRef(0)

  const load = useCallback((signal?: AbortSignal) => {
    const sequence = ++loadSequence.current
    return api.networks(signal)
      .then(async (list) => {
        // Only managed networks have config worth expanding.
        const loaded = await Promise.all(list.filter((n) => n.managed)
          .map((n) => api.network(n.name, signal).catch(() => null)))
        if (sequence !== loadSequence.current) return
        const next: Record<string, NetworkDetail> = {}
        loaded.forEach((detail) => { if (detail) next[detail.name] = detail })
        setSummaries(list)
        setDetails(next)
        setError(null)
      })
      .catch((cause) => {
        if ((cause as Error).name !== 'AbortError' && sequence === loadSequence.current) {
          setError((cause as Error).message)
        }
      })
  }, [])

  useEffect(() => {
    const controller = new AbortController()
    load(controller.signal)
    const timer = window.setInterval(() => { if (!mutating.current) load() }, 5000)
    return () => { controller.abort(); window.clearInterval(timer) }
  }, [load])

  const run = useCallback(async (action: () => Promise<unknown>, success: string) => {
    mutating.current = true
    setBusy(true)
    try {
      await action()
      onNotify('success', success)
      setDialog(null)
      setPendingDelete(null)
    } catch (cause) {
      onNotify('error', 'Network change failed', (cause as Error).message)
    } finally {
      await load()
      setBusy(false)
      mutating.current = false
    }
  }, [load, onNotify])

  if (error && !summaries) {
    return (
      <div className="banner banner-error">
        <div className="banner-body"><p style={{ margin: 0 }}>{error}</p></div>
      </div>
    )
  }
  if (!summaries) {
    return (
      <div className="card">
        <div className="loading-wrap"><span className="spinner" /> Loading networks…</div>
      </div>
    )
  }

  const managed = summaries.filter((n) => n.managed)
  const others = summaries.filter((n) => !n.managed)

  return (
    <>
      {error && (
        <div className="banner banner-error"><div className="banner-body"><p>{error}</p></div></div>
      )}

      <FabricsSection onNotify={onNotify} onChanged={load} />

      <div className="storage-toolbar">
        <span className="faint" style={{ fontSize: 12.5 }}>
          {managed.length} managed · {others.length} host interface{others.length === 1 ? '' : 's'}
        </span>
        <button className="btn btn-primary" disabled={busy || !canWrite}
          onClick={() => setDialog('new')}>
          <PlusIcon /> New network
        </button>
      </div>

      {managed.length === 0 && (
        <div className="card net-card">
          <div className="empty">
            <h3>No managed networks</h3>
            <p>Create a bridge, or run setup to create one and attach it to the default profile.</p>
          </div>
        </div>
      )}

      {managed.map((summary) => {
        const detail = details[summary.name]
        return (
          <section className="card net-card" key={summary.name}>
            <header className="net-head">
              <h3>{summary.name}</h3>
              <span className="badge badge-info">{summary.type}</span>
              {summary.status && <span className="badge badge-ok">{summary.status}</span>}
              {summary.default && (
                <span className="badge badge-dim"
                  title="The default profile's NIC joins this network, so new instances do too">
                  default
                </span>
              )}
              {summary.description && <span className="faint net-desc">{summary.description}</span>}
              <span className="net-head-end">
                {detail && (
                  <span className="faint net-traffic num">
                    ↓ {bytes(detail.counters.rx)} · ↑ {bytes(detail.counters.tx)}
                  </span>
                )}
                {detail?.manageable ? (
                  <>
                    <button className="btn btn-sm" disabled={busy || !canWrite}
                      onClick={() => setDialog(detail)}>Edit</button>
                    <button className="btn btn-sm btn-danger" disabled={busy || !canWrite}
                      onClick={() => setPendingDelete(detail)}>Delete</button>
                  </>
                ) : detail && (
                  <span className="badge badge-dim" title={detail.read_only_reason}>read-only</span>
                )}
              </span>
            </header>

            {!detail ? (
              <div className="loading-wrap"><span className="spinner" /></div>
            ) : (
              <NetworkBody detail={detail} />
            )}
          </section>
        )
      })}

      {others.length > 0 && (
        <section className="card net-card">
          <header className="net-head">
            <h3>Other host interfaces</h3>
            <span className="faint">not managed by the daemon</span>
          </header>
          <div className="table-scroll">
            <table className="ctable net-table">
              <thead>
                <tr><th>Name</th><th>Type</th><th>Used by</th><th>New instances</th></tr>
              </thead>
              <tbody>
                {others.map((network) => (
                  <tr key={network.name}>
                    <td className="mono">{network.name}</td>
                    <td className="dim">{network.type}</td>
                    <td className="dim">{network.used_by || '—'}</td>
                    <td>
                      {network.attachable
                        ? <span className="badge badge-ok">can join</span>
                        : <span className="faint">—</span>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="hint" style={{ padding: '0 14px 12px' }}>
            These exist on the host but the daemon does not configure them. A new
            instance can still be put on a host bridge from the create dialog; it
            then gets its address from whatever serves that network.
          </p>
        </section>
      )}

      {dialog && (
        <NetworkDialog
          network={dialog === 'new' ? null : dialog}
          busy={busy}
          onCancel={() => setDialog(null)}
          onSubmit={(request) => dialog === 'new'
            ? run(() => api.createNetwork(request), `Created network ${request.name}`)
            : run(() => api.updateNetwork(dialog.name, request), `Updated network ${dialog.name}`)}
        />
      )}

      {pendingDelete && (
        <NetworkDeleteDialog
          network={pendingDelete}
          busy={busy}
          onCancel={() => setPendingDelete(null)}
          onConfirm={() => run(() => api.deleteNetwork(pendingDelete.name),
            `Deleted network ${pendingDelete.name}`)}
        />
      )}
    </>
  )
}

/**
 * Fabrics: routed networks shared by every node, each a /24 per node out of a
 * prefix the cluster shares. Always shown, since this is where one is made;
 * each fabric's table is every node's own report of its part, so a missing
 * bridge or route on another host shows here rather than as silent loss.
 */
function FabricsSection({ onNotify, onChanged }: {
  onNotify: (kind: ToastKind, title: string, detail?: string) => void
  onChanged: () => void
}) {
  const canWrite = useCanWrite()
  const [overview, setOverview] = useState<FabricOverview | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [plan, setPlan] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [creating, setCreating] = useState(false)
  const [pendingDelete, setPendingDelete] = useState<FabricSummary | null>(null)
  const busyRef = useRef(false)

  const load = useCallback((signal?: AbortSignal) => {
    return api.fabrics(signal)
      .then((next) => { setOverview(next); setError(null) })
      .catch((cause) => {
        if ((cause as Error).name !== 'AbortError') setError((cause as Error).message)
      })
  }, [])

  // Its own 10s interval rather than the tab's 5s one: every poll asks each
  // member for its bridges and routes, and none of that moves that fast.
  // A slow member can hold a poll past the next tick, so a tick is skipped
  // while one is out rather than stacking requests behind it.
  useEffect(() => {
    const controller = new AbortController()
    let polling = true
    load(controller.signal).finally(() => { polling = false })
    const timer = window.setInterval(() => {
      if (busyRef.current || polling) return
      polling = true
      load(controller.signal).finally(() => { polling = false })
    }, 10000)
    return () => { controller.abort(); window.clearInterval(timer) }
  }, [load])

  const act = async <T,>(run: () => Promise<T>, report: (result: T) => void,
    failure: string) => {
    busyRef.current = true
    setBusy(true)
    try {
      report(await run())
      setCreating(false)
      setPendingDelete(null)
      setPlan(null)
    } catch (err) {
      onNotify('error', failure, (err as Error).message)
    } finally {
      await load()
      // Creating or deleting a fabric adds or removes a bridge here, so the
      // network list below is out of date too.
      onChanged()
      busyRef.current = false
      setBusy(false)
    }
  }

  const reportNodes = (verb: string, result: FabricChangeResult | FabricDeleteResult) => {
    const failed = result.nodes.filter((n) => !n.ok)
    if (failed.length === 0) {
      onNotify('success', `Fabric ${result.name} ${verb}`,
        `${result.nodes.length} node${result.nodes.length === 1 ? '' : 's'}`)
    } else {
      onNotify('error', `Fabric ${result.name} ${verb}, but not everywhere`,
        failed.map((n) => `${n.node}: ${n.error}`).join('\n'))
    }
  }

  if (!overview) {
    return error ? (
      <div className="banner banner-error"><div className="banner-body"><p>{error}</p></div></div>
    ) : null
  }

  const local = overview.local
  const silent = overview.nodes.filter((n) => !n.ok)

  return (
    <>
      <section className="card net-card">
        <header className="net-head">
          <h3>Fabrics</h3>
          <span className="faint net-desc">
            routed between nodes · {overview.fabrics.length} fabric
            {overview.fabrics.length === 1 ? '' : 's'} · {overview.nodes.length} node
            {overview.nodes.length === 1 ? '' : 's'}
          </span>
          {!local.privileged && overview.fabrics.length > 0 && (
            <span className="badge badge-dim"
              title="lemondx cannot program routes here; the plan shows what to run">
              no privilege here
            </span>
          )}
          <span className="net-head-end">
            {local.pending > 0 && canWrite && local.privileged && (
              <button className="btn btn-sm" disabled={busy}
                onClick={() => act(() => api.fabricApply(), (r) => r.ok
                  ? onNotify('success', 'Fabric routes updated on this node')
                  : onNotify('error', 'Some fabric commands failed',
                    r.applied.filter((c) => !c.ok).map((c) => c.error).join('\n')),
                'Could not apply the fabric')}>
                Apply {local.pending} change{local.pending === 1 ? '' : 's'}
              </button>
            )}
            {overview.fabrics.length > 0 && (
              <button className="btn btn-sm" disabled={busy}
                onClick={() => {
                  if (plan !== null) { setPlan(null); return }
                  api.fabricPlan().then((p) => setPlan(p.text || 'Nothing to do.'))
                    .catch((err) => onNotify('error', 'Could not read the plan', err.message))
                }}>
                {plan !== null ? 'Hide plan' : 'Show plan'}
              </button>
            )}
            <button className="btn btn-sm btn-primary"
              disabled={busy || !canWrite || silent.length > 0}
              title={silent.length > 0
                ? `Every node must answer to create a fabric; ${silent.map((n) => n.node).join(', ')} does not`
                : undefined}
              onClick={() => setCreating(true)}>
              <PlusIcon /> New fabric
            </button>
          </span>
        </header>

        <div className="net-section">
          <p className="net-explain">
            A fabric is one bridge on every node, each on its own /24 of a shared
            prefix, with host routes between them. Its instances reach each other
            across nodes by their own addresses, and nothing outside the fabric can
            reach in. Traffic for anywhere else is NAT&apos;d behind the host,
            unless the fabric was made without NAT.
            {!overview.clustered && ' This node is not in a cluster, so a fabric here spans this host only.'}
          </p>
          {plan !== null && <pre className="net-plan">{plan}</pre>}
        </div>

        {silent.length > 0 && (
          <div className="banner banner-warn"><div className="banner-body">
            <p>
              <strong>{silent.map((n) => n.node).join(', ')}</strong>{' '}
              {silent.length === 1 ? 'is' : 'are'} not answering, so no fabric can be
              created until {silent.length === 1 ? 'it is' : 'they are'} back: a fabric
              is made on every node at once, and a node that cannot be asked what it
              already uses cannot be given a subnet safely.
            </p>
            <p style={{ marginTop: 6 }}>
              If {silent.length === 1 ? 'it is' : 'they are'} gone for good, evict{' '}
              {silent.length === 1 ? 'it' : 'them'} on the <strong>Nodes</strong> tab.
              Eviction works on a node that cannot be reached (the cluster credential
              is rotated instead of the node being told). The rows below show{' '}
              {silent.length === 1 ? 'its' : 'their'} last known claims meanwhile.
            </p>
          </div></div>
        )}
        {local.error && (
          <div className="banner banner-error"><div className="banner-body"><p>{local.error}</p></div></div>
        )}
        {local.check && (
          <div className="banner banner-warn"><div className="banner-body"><p>{local.check}</p></div></div>
        )}
        {local.firewall && !local.firewall.ok && (
          <div className="banner banner-warn"><div className="banner-body"><p>
            The fabric firewall is not in place on this node: {local.firewall.error}
          </p></div></div>
        )}
        {local.warnings.map((warning) => (
          <div className="banner banner-warn" key={warning}>
            <div className="banner-body"><p style={{ whiteSpace: 'pre-wrap' }}>{warning}</p></div>
          </div>
        ))}
      </section>

      {overview.fabrics.map((fabric) => (
        <FabricCard key={fabric.name} fabric={fabric} busy={busy} canWrite={canWrite}
          onExtend={() => act(() => api.extendFabric(fabric.name),
            (r) => reportNodes('extended', r), `Could not extend ${fabric.name}`)}
          onDelete={() => setPendingDelete(fabric)} />
      ))}

      {creating && (
        <FabricDialog busy={busy} onCancel={() => setCreating(false)}
          onSubmit={(request) => act(() => api.createFabric(request),
            (r) => reportNodes('created', r), 'Could not create the fabric')} />
      )}

      {pendingDelete && (
        <FabricDeleteDialog fabric={pendingDelete} busy={busy}
          onCancel={() => setPendingDelete(null)}
          onConfirm={() => act(() => api.deleteFabric(pendingDelete.name),
            (r) => reportNodes('deleted', r), `Could not delete ${pendingDelete.name}`)} />
      )}
    </>
  )
}

const MEMBER_BADGE: Record<FabricMember['state'], string> = {
  ok: 'badge-ok', absent: 'badge-dim', unknown: 'badge-warn', bridge: 'badge-warn',
  routes: 'badge-warn', conflict: 'badge-danger',
}

const MEMBER_LABEL: Record<FabricMember['state'], string> = {
  ok: 'ok', absent: 'not on it', unknown: 'no answer', bridge: 'no bridge',
  routes: 'routes', conflict: 'conflict',
}

function FabricCard({ fabric, busy, canWrite, onExtend, onDelete }: {
  fabric: FabricSummary
  busy: boolean
  canWrite: boolean
  onExtend: () => void
  onDelete: () => void
}) {
  const troubled = fabric.members.filter((m) => m.state !== 'ok')
  const absent = fabric.members.filter((m) => m.state === 'absent')
  return (
    <section className="card net-card">
      <header className="net-head">
        <h3 className="mono">{fabric.name}</h3>
        <span className="badge badge-info mono">{fabric.prefix}</span>
        <span className={`badge ${fabric.nat ? 'badge-ok' : 'badge-dim'}`}
          title={fabric.nat
            ? "Traffic leaving the fabric is NAT'd behind its host"
            : 'Nothing leaves the fabric: instances need another NIC for the internet'}>
          NAT {fabric.nat ? 'on' : 'off'}
        </span>
        {troubled.length === 0 ? (
          <span className="badge badge-ok">on every node</span>
        ) : (
          <span className={`badge ${fabric.conflict.length ? 'badge-danger' : 'badge-warn'}`}>
            {troubled.length} of {fabric.members.length} node
            {fabric.members.length === 1 ? '' : 's'} need attention
          </span>
        )}
        <span className="faint net-desc">
          {fabric.instances} instance{fabric.instances === 1 ? '' : 's'}
        </span>
        <span className="net-head-end">
          {absent.length > 0 && (
            <button className="btn btn-sm" disabled={busy || !canWrite}
              title={`Allocate a /24 to ${absent.map((m) => m.node).join(', ')}`}
              onClick={onExtend}>
              Add missing node{absent.length === 1 ? '' : 's'}
            </button>
          )}
          <button className="btn btn-sm btn-danger" disabled={busy || !canWrite}
            onClick={onDelete}>Delete</button>
        </span>
      </header>
      <div className="net-section">
        <div className="table-scroll">
          <table className="ctable net-table">
            <thead>
              <tr>
                <th>Node</th><th>Subnet</th><th className="optional">Gateway</th>
                <th>Routes</th><th>Instances</th><th>State</th>
              </tr>
            </thead>
            <tbody>
              {fabric.members.map((member) => (
                <tr key={member.node}>
                  <td>{member.node}</td>
                  <td className="mono num">{member.subnet || '—'}</td>
                  <td className="optional mono faint">{member.gateway || '—'}</td>
                  <td className="num">
                    {member.subnet && member.state !== 'unknown'
                      ? `${member.routes_ok}/${member.routes_total}` : '—'}
                  </td>
                  <td className="dim">
                    {member.instances.length ? member.instances.join(', ') : '—'}
                  </td>
                  <td>
                    <span className={`badge ${MEMBER_BADGE[member.state]}`} title={member.detail}>
                      {MEMBER_LABEL[member.state]}
                    </span>
                    {member.state !== 'ok' && member.state !== 'absent' && member.detail && (
                      <div className="faint" style={{ fontSize: 11.5, marginTop: 3 }}>
                        {member.detail}
                      </div>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </section>
  )
}

/**
 * Creating a fabric. The name and prefix start as the server's suggestions,
 * and every edit is checked against every node -- interfaces and routes, not
 * just this host's -- so the dialog shows each node's /24 before anything is
 * made, and refuses exactly what the server would.
 */
function FabricDialog({ busy, onCancel, onSubmit }: {
  busy: boolean
  onCancel: () => void
  onSubmit: (request: { name: string; prefix: string; nat: boolean }) => void
}) {
  const [name, setName] = useState('')
  const [prefix, setPrefix] = useState('')
  const [nat, setNat] = useState(true)
  const [check, setCheck] = useState<FabricCheck | null>(null)
  // The inputs the check in hand answers, so a stale one cannot enable Create.
  const [checkedFor, setCheckedFor] = useState<string | null>(null)
  const [checkError, setCheckError] = useState('')
  const [started, setStarted] = useState(false)

  // The first check asks for suggestions and fills both boxes with them.
  useEffect(() => {
    const controller = new AbortController()
    api.fabricCheck('', '', controller.signal).then((result) => {
      setName(result.name)
      setPrefix(result.prefix)
      setCheck(result)
      setCheckedFor(`${result.name}|${result.prefix}`)
      setStarted(true)
    }).catch((cause) => {
      if ((cause as Error).name !== 'AbortError') {
        setCheckError((cause as Error).message)
        setStarted(true)
      }
    })
    return () => controller.abort()
  }, [])

  const key = `${name.trim()}|${prefix.trim()}`
  useEffect(() => {
    if (!started || key === checkedFor) return
    const controller = new AbortController()
    const timer = window.setTimeout(() => {
      api.fabricCheck(name.trim(), prefix.trim(), controller.signal).then((result) => {
        setCheck(result)
        setCheckedFor(key)
        setCheckError('')
      }).catch((cause) => {
        if ((cause as Error).name !== 'AbortError') setCheckError((cause as Error).message)
      })
    }, 350)
    return () => { controller.abort(); window.clearTimeout(timer) }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, started])

  const current = check !== null && checkedFor === key
  const canSubmit = !busy && current && check.ok

  function submit(event: FormEvent) {
    event.preventDefault()
    if (canSubmit) onSubmit({ name: name.trim(), prefix: check.prefix, nat })
  }

  const allocation = check ? Object.entries(check.allocation).sort(([a], [b]) => a.localeCompare(b)) : []

  return (
    <Modal
      title="Create fabric"
      subtitle="A bridge on every node, each on its own /24 of one prefix, routed between them. Every node in the cluster must be reachable."
      onClose={busy ? () => {} : onCancel}
      // The server's check normalises the prefix, and that is what is sent.
      api={calls.createFabric({
        name: name.trim(), prefix: current ? check.prefix : prefix.trim(), nat })}
      footer={
        <>
          <button type="button" className="btn" onClick={onCancel} disabled={busy}>Cancel</button>
          <button className="btn btn-primary" form="fabric-form" disabled={!canSubmit}>
            {busy && <span className="spinner" />}
            Create on {allocation.length || 'every'} node{allocation.length === 1 ? '' : 's'}
          </button>
        </>
      }
    >
      <form id="fabric-form" className="storage-form" onSubmit={submit}>
        <div className="grid-2">
          <Field label="Name">
            <input className="input mono" value={name} disabled={busy || !started}
              maxLength={15} autoComplete="off" placeholder="lemonfab1"
              onChange={(event) => setName(event.target.value)} />
            <span className="hint">Also the bridge&apos;s name on every node.</span>
            {current && check.name_error && <span className="field-error">{check.name_error}</span>}
          </Field>
          <Field label="Prefix">
            <input className="input mono" value={prefix} disabled={busy || !started}
              autoComplete="off" placeholder="10.101.0.0/16"
              onChange={(event) => setPrefix(event.target.value)} />
            <span className="hint">
              A private /16 to /22; each node gets a /24.{' '}
              {prefix.trim() !== '' && (
                <button type="button" className="link-btn" disabled={busy}
                  onClick={() => setPrefix('')}>Suggest a free one</button>
              )}
            </span>
            {current && check.prefix_error && (
              <span className="field-error">{check.prefix_error}</span>
            )}
          </Field>
        </div>

        <label className="check">
          <input type="checkbox" checked={nat} disabled={busy}
            onChange={(event) => setNat(event.target.checked)} />
          <span>
            NAT traffic that leaves the fabric
            <span className="hint">
              {nat
                ? ' Instances can use the fabric as their only NIC and still reach the internet.'
                : ' Instances reach only each other through it; nothing else is routed.'}
            </span>
          </span>
        </label>

        {checkError && (
          <div className="banner banner-error"><div className="banner-body"><p>{checkError}</p></div></div>
        )}
        {current && check.maintenance_error && (
          <div className="banner banner-warn"><div className="banner-body"><p>
            {check.maintenance_error}
          </p></div></div>
        )}
        {current && check.unreachable_error && (
          <div className="banner banner-error"><div className="banner-body"><p>
            {check.unreachable_error}
          </p></div></div>
        )}

        <div className="net-section" style={{ padding: 0 }}>
          <h4>
            What each node gets
            {!current && started && <span className="faint"> · checking…</span>}
          </h4>
          {!started ? (
            <div className="loading-wrap"><span className="spinner" /> Asking every node what it uses…</div>
          ) : allocation.length === 0 ? (
            <p className="hint">Nothing to show until the name and prefix fit.</p>
          ) : (
            <div className="table-scroll">
              <table className="ctable net-table">
                <thead><tr><th>Node</th><th>Subnet</th><th>Gateway</th></tr></thead>
                <tbody>
                  {allocation.map(([node, subnet]) => (
                    <tr key={node} style={current ? undefined : { opacity: 0.5 }}>
                      <td>{node}</td>
                      <td className="mono num">{subnet}</td>
                      <td className="mono faint">{subnet.replace(/\.0\/24$/, '.1')}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {current && check.ok && (
            <p className="hint" style={{ marginTop: 8 }}>
              Checked against every interface and route on {allocation.length} node
              {allocation.length === 1 ? '' : 's'}; nothing overlaps.
            </p>
          )}
        </div>
      </form>
    </Modal>
  )
}

function FabricDeleteDialog({ fabric, busy, onCancel, onConfirm }: {
  fabric: FabricSummary
  busy: boolean
  onCancel: () => void
  onConfirm: () => void
}) {
  const inUse = fabric.members.filter((m) => m.instances.length > 0)
  const silent = fabric.members.filter((m) => m.state === 'unknown')
  const holding = fabric.members.filter((m) => m.state !== 'absent')
  return (
    <Modal
      title={`Delete fabric ${fabric.name}?`}
      onClose={busy ? () => {} : onCancel}
      api={calls.deleteFabric(fabric.name)}
      footer={
        <>
          <button className="btn" onClick={onCancel} disabled={busy}>Cancel</button>
          <button className="btn btn-danger" onClick={onConfirm}
            disabled={busy || inUse.length > 0}>
            {busy && <span className="spinner" />}
            Delete everywhere
          </button>
        </>
      }
    >
      {inUse.length > 0 ? (
        <div className="pool-delete-dialog">
          <p style={{ margin: 0 }}>
            Detach or delete these instances first. lemondx does not do it for you:
            an instance whose NIC is removed keeps running cut off, which is easy to miss.
          </p>
          {inUse.map((member) => (
            <div className="pool-delete-group" key={member.node}>
              <strong>{member.node}</strong>
              <ul>{member.instances.map((i) => <li key={i} className="mono">{i}</li>)}</ul>
            </div>
          ))}
        </div>
      ) : (
        <p style={{ margin: 0, fontSize: 13.5, lineHeight: 1.6 }}>
          This removes the bridge <span className="mono">{fabric.name}</span>, its routes
          and its firewall rules from {holding.map((m) => m.node).join(', ') || 'every node'},
          and frees <span className="mono">{fabric.prefix}</span>.
          {silent.length > 0 && (
            <> {silent.map((m) => m.node).join(', ')} did not answer and will keep
              {silent.length === 1 ? ' its' : ' their'} part until you delete again.</>
          )}
        </p>
      )}
    </Modal>
  )
}

function NetworkBody({ detail }: { detail: NetworkDetail }) {
  return (
    <>
      <div className="net-grid">
        <Family label="IPv4" family={detail.ipv4} />
        <Family label="IPv6" family={detail.ipv6} />
        <Fact label="DNS domain" value={detail.dns_domain || '—'} mono />
        <Fact label="MTU" value={String(detail.mtu || '—')} />
        <Fact label="Bridge MAC" value={detail.hwaddr || '—'} mono />
        <Fact label="Link" value={detail.state || '—'} />
      </div>

      <div className="net-section">
        <h4>How containers reach it</h4>
        <p className="net-explain">
          {detail.ipv4.address && detail.ipv4.address !== 'none' ? (
            <>
              Containers get an address on{' '}
              <span className="mono">{detail.ipv4.address}</span>
              {detail.ipv4.dhcp ? ' by DHCP' : ' statically'}, with{' '}
              <span className="mono">{detail.ipv4.address.split('/')[0]}</span>{' '}
              as their gateway and DNS resolver
              {detail.dns_domain && (
                <> in the <span className="mono">{detail.dns_domain}</span> domain</>
              )}
              .{' '}
              {detail.ipv4.nat
                ? 'Outbound traffic is NATed behind the host, so containers can reach the internet but are not reachable from outside.'
                : 'NAT is off, so outbound traffic needs a route on your network — bootstrap modules that download packages will fail without one.'}
            </>
          ) : (
            <>This network hands out no IPv4 addresses.</>
          )}
        </p>
        {detail.profiles.length > 0 || detail.instances.length > 0 ? (
          <p className="net-explain">
            Attached{' '}
            {detail.profiles.length > 0 && (
              <>via profile{detail.profiles.length > 1 ? 's' : ''}{' '}
                <span className="mono">{detail.profiles.join(', ')}</span></>
            )}
            {detail.profiles.length > 0 && detail.instances.length > 0 && ', plus '}
            {detail.instances.length > 0 && (
              <>directly by <span className="mono">{detail.instances.join(', ')}</span></>
            )}
            .
          </p>
        ) : (
          <p className="net-explain">
            Nothing is attached yet. Pick it under <strong>Network</strong> when
            creating an instance.
          </p>
        )}
      </div>

      <div className="net-section">
        <h4>
          Addresses in use
          <span className="faint"> · {detail.leases.length}</span>
        </h4>
        {detail.leases.length === 0 ? (
          <p className="hint">No leases yet — nothing is running on this bridge.</p>
        ) : (
          <div className="table-scroll">
            <table className="ctable net-table">
              <thead>
                <tr>
                  <th>Name</th><th>Address</th>
                  <th className="optional">MAC</th><th>Kind</th>
                </tr>
              </thead>
              <tbody>
                {detail.leases.map((lease) => (
                  <tr key={`${lease.address}-${lease.hwaddr}`}>
                    <td>{lease.hostname || '—'}</td>
                    <td className="mono num">{lease.address}</td>
                    <td className="optional mono faint">{lease.hwaddr || '—'}</td>
                    <td>
                      <span className={`badge ${
                        lease.type === 'gateway' ? 'badge-info' : 'badge-dim'}`}>
                        {lease.type}
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </>
  )
}

// Config keys the dialog has its own fields for; anything else set on the
// network is shown, and edited, as an advanced KEY=VALUE line.
const FIELD_KEYS = new Set([
  'ipv4.address', 'ipv4.nat', 'ipv4.dhcp', 'ipv6.address', 'ipv6.nat', 'ipv6.dhcp',
  'dns.domain', 'bridge.mtu',
])

// What the daemon assumes for a key that is not set, so an edit that leaves a
// field alone does not write the default out explicitly.
const IMPLICIT: Record<string, string> = {
  'ipv4.nat': 'false', 'ipv6.nat': 'false', 'ipv4.dhcp': 'true', 'ipv6.dhcp': 'true',
  'ipv4.address': 'none', 'ipv6.address': 'none',
}

/** How a stored address starts out in the field. */
function initialSubnet(stored: string | undefined, family: 4 | 6, fallback: string) {
  if (stored === undefined) return fallback
  return stored === 'auto' || stored === 'none' ? stored : displayAddress(stored, family)
}

function NetworkDialog({ network, busy, onCancel, onSubmit }: {
  network: NetworkDetail | null
  busy: boolean
  onCancel: () => void
  onSubmit: (request: NetworkRequest) => void
}) {
  const current = network?.config ?? {}
  const [name, setName] = useState(network?.name ?? '')
  const [description, setDescription] = useState(network?.description ?? '')
  const [v4, setV4] = useState(initialSubnet(current['ipv4.address'], 4, network ? 'none' : 'auto'))
  const [v4Nat, setV4Nat] = useState(network ? network.ipv4.nat : true)
  const [v4Dhcp, setV4Dhcp] = useState(network ? network.ipv4.dhcp : true)
  const [v6, setV6] = useState(initialSubnet(current['ipv6.address'], 6, 'none'))
  const [v6Nat, setV6Nat] = useState(network ? network.ipv6.nat : true)
  const [v6Dhcp, setV6Dhcp] = useState(network ? network.ipv6.dhcp : true)
  const [dnsDomain, setDnsDomain] = useState(current['dns.domain'] ?? '')
  const [mtu, setMtu] = useState(current['bridge.mtu'] ?? '')
  const initialAdvanced = Object.entries(current)
    .filter(([key]) => !FIELD_KEYS.has(key))
    .map(([key, value]) => `${key}=${value}`).join('\n')
  const [advanced, setAdvanced] = useState(initialAdvanced)
  const [formError, setFormError] = useState('')
  const [used, setUsed] = useState<SubnetInUse[] | null>(null)

  // The network being edited may keep its own subnet, so it is left out.
  useEffect(() => {
    const controller = new AbortController()
    api.subnets(controller.signal)
      .then((list) => setUsed(list.filter((entry) => entry.interface !== network?.name)))
      .catch(() => setUsed([]))
    return () => controller.abort()
  }, [network?.name])

  const v4Status = subnetStatus(v4, 4, used)
  const v6Status = subnetStatus(v6, 6, used)

  function desired(): Record<string, string> {
    const config: Record<string, string> = {
      'ipv4.address': v4Status.address ?? '',
      'ipv6.address': v6Status.address ?? '',
      'dns.domain': dnsDomain.trim(),
      'bridge.mtu': mtu.trim(),
    }
    if (v4 !== 'none') {
      config['ipv4.nat'] = String(v4Nat)
      config['ipv4.dhcp'] = String(v4Dhcp)
    }
    if (v6 !== 'none') {
      config['ipv6.nat'] = String(v6Nat)
      config['ipv6.dhcp'] = String(v6Dhcp)
    }
    const extra = parseConfig(advanced)
    for (const key of Object.keys(parseConfig(initialAdvanced))) {
      if (!(key in extra)) extra[key] = ''
    }
    return { ...config, ...extra }
  }

  /** Throws while the advanced options do not parse. */
  function request(): NetworkRequest {
    const config = desired()
    if (!network) {
      return {
        name: name.trim(),
        description: description.trim(),
        config: Object.fromEntries(Object.entries(config).filter(([, value]) => value !== '')),
      }
    }
    // Only what changed: re-sending keys the daemon has but lemondx does not
    // allow (set with the daemon's own CLI, say) would be refused.
    const changes = Object.fromEntries(Object.entries(config).filter(([key, value]) =>
      value !== (current[key] ?? IMPLICIT[key] ?? '')))
    return { description: description.trim(), config: changes }
  }

  function submit(event: FormEvent) {
    event.preventDefault()
    try {
      onSubmit(request())
    } catch (cause) {
      setFormError((cause as Error).message)
    }
  }

  let call: ApiCall | null = null
  try {
    call = network ? calls.updateNetwork(network.name, request())
      : calls.createNetwork(request())
  } catch { /* shown as "fill in the form" until the options parse */ }

  const canSubmit = !busy && (network || name.trim())
    && !v4Status.error && !v6Status.error

  return (
    <Modal
      title={network ? `Edit ${network.name}` : 'Create network'}
      subtitle={network ? undefined
        : 'A managed bridge: the daemon runs DHCP and DNS on it and can NAT it behind the host.'}
      onClose={busy ? () => {} : onCancel}
      api={call}
      footer={
        <>
          <button type="button" className="btn" onClick={onCancel} disabled={busy}>Cancel</button>
          <button className="btn btn-primary" form="network-form" disabled={!canSubmit}>
            {busy && <span className="spinner" />}
            {network ? 'Save changes' : 'Create network'}
          </button>
        </>
      }
    >
      <form id="network-form" className="storage-form" onSubmit={submit}>
        <div className="grid-2">
          <Field label="Name">
            <input className="input mono" required value={name} disabled={!!network || busy}
              maxLength={15} placeholder="lxdbr1" autoComplete="off"
              onChange={(event) => setName(event.target.value)} />
          </Field>
          <Field label="Description">
            <input className="input" value={description} disabled={busy}
              onChange={(event) => setDescription(event.target.value)} />
          </Field>
        </div>

        <FamilyFields family={4} value={v4} nat={v4Nat} dhcp={v4Dhcp} used={used} disabled={busy}
          onChange={setV4} onNat={setV4Nat} onDhcp={setV4Dhcp} />
        <FamilyFields family={6} value={v6} nat={v6Nat} dhcp={v6Dhcp} used={used} disabled={busy}
          onChange={setV6} onNat={setV6Nat} onDhcp={setV6Dhcp} />

        <div className="grid-2">
          <Field label="DNS domain">
            <input className="input mono" value={dnsDomain} disabled={busy}
              placeholder={network?.dns_domain || 'lxd'} autoComplete="off"
              onChange={(event) => setDnsDomain(event.target.value)} />
          </Field>
          <Field label="MTU">
            <input className="input" value={mtu} disabled={busy} inputMode="numeric"
              placeholder="1500" autoComplete="off"
              onChange={(event) => setMtu(event.target.value)} />
          </Field>
        </div>

        <Field label="Advanced bridge options">
          <textarea className="textarea mono" rows={3} value={advanced} disabled={busy}
            placeholder="ipv4.dhcp.ranges=10.10.0.100-10.10.0.199"
            onChange={(event) => { setAdvanced(event.target.value); setFormError('') }} />
          {formError && <span className="field-error">{formError}</span>}
        </Field>
        {network && network.instances.length > 0 && (
          <p className="hint" style={{ margin: 0 }}>
            If you change the subnet, {network.instances.join(', ')} keep their
            old address until their DHCP lease renews; restart them to move over at once.
          </p>
        )}
      </form>
    </Modal>
  )
}

function FamilyFields({
  family, value, nat, dhcp, used, disabled, onChange, onNat, onDhcp,
}: {
  family: 4 | 6
  value: string
  nat: boolean
  dhcp: boolean
  used: SubnetInUse[] | null
  disabled: boolean
  onChange: (value: string) => void
  onNat: (value: boolean) => void
  onDhcp: (value: boolean) => void
}) {
  return (
    <fieldset className="net-family">
      <legend>IPv{family}</legend>
      <SubnetField id={`net-ipv${family}`} family={family} value={value} used={used}
        disabled={disabled} onChange={onChange} />
      {value !== 'none' && (
        <div className="net-family-flags">
          <label className="checkbox">
            <input type="checkbox" checked={nat} disabled={disabled}
              onChange={(event) => onNat(event.target.checked)} />
            NAT behind the host
          </label>
          <label className="checkbox">
            <input type="checkbox" checked={dhcp} disabled={disabled}
              onChange={(event) => onDhcp(event.target.checked)} />
            DHCP
          </label>
        </div>
      )}
    </fieldset>
  )
}

function NetworkDeleteDialog({ network, busy, onCancel, onConfirm }: {
  network: NetworkDetail
  busy: boolean
  onCancel: () => void
  onConfirm: () => void
}) {
  const inUse = network.instances.length > 0 || network.profiles.length > 0
  return (
    <Modal
      title={`Delete network ${network.name}?`}
      onClose={busy ? () => {} : onCancel}
      api={calls.deleteNetwork(network.name)}
      footer={
        <>
          <button className="btn" onClick={onCancel} disabled={busy}>Cancel</button>
          <button className="btn btn-danger" onClick={onConfirm} disabled={busy || inUse}>
            {busy && <span className="spinner" />}
            Delete network
          </button>
        </>
      }
    >
      {inUse ? (
        <div className="pool-delete-dialog">
          <p style={{ margin: 0 }}>
            Detach everything from <strong className="mono">{network.name}</strong> first.
            lemondx does not do it for you: an instance whose NIC is removed keeps
            running with no network, which is easy to miss.
          </p>
          {network.profiles.length > 0 && (
            <div className="pool-delete-group">
              <strong>Profiles</strong>
              <ul>{network.profiles.map((p) => <li key={p} className="mono">{p}</li>)}</ul>
            </div>
          )}
          {network.instances.length > 0 && (
            <div className="pool-delete-group">
              <strong>Instances</strong>
              <ul>{network.instances.map((i) => <li key={i} className="mono">{i}</li>)}</ul>
            </div>
          )}
        </div>
      ) : (
        <p style={{ margin: 0, fontSize: 13.5, lineHeight: 1.6 }}>
          This removes the bridge <span className="mono">{network.name}</span> and its
          DHCP and DNS service from the host. Nothing is attached to it.
        </p>
      )}
    </Modal>
  )
}

function Field({ label, children }: { label: string; children: ReactNode }) {
  return <label className="field"><span>{label}</span>{children}</label>
}

function parseConfig(text: string): Record<string, string> {
  const config: Record<string, string> = {}
  for (const [index, line] of text.split('\n').entries()) {
    const trimmed = line.trim()
    if (!trimmed) continue
    const separator = trimmed.indexOf('=')
    if (separator < 1) throw new Error(`Advanced option on line ${index + 1} must be KEY=VALUE.`)
    config[trimmed.slice(0, separator).trim()] = trimmed.slice(separator + 1).trim()
  }
  return config
}

function Fact({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="net-fact">
      <div className="stat-label">{label}</div>
      <div className={`net-fact-value${mono ? ' mono' : ''}`}>{value}</div>
    </div>
  )
}

function Family({ label, family }: { label: string; family: NetworkFamily }) {
  const off = !family.address || family.address === 'none'
  return (
    <div className="net-fact">
      <div className="stat-label">{label}</div>
      <div className="net-fact-value mono">{off ? 'disabled' : family.address}</div>
      {!off && (
        <div className="net-fact-tags">
          <span className={`badge ${family.nat ? 'badge-ok' : 'badge-dim'}`}>
            NAT {family.nat ? 'on' : 'off'}
          </span>
          <span className={`badge ${family.dhcp ? 'badge-ok' : 'badge-dim'}`}>
            DHCP {family.dhcp ? 'on' : 'off'}
          </span>
        </div>
      )}
    </div>
  )
}
