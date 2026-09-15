import { useCallback, useEffect, useRef, useState } from 'react'
import { useCanWrite } from '../hooks/useAuth'
import type { FormEvent, ReactNode } from 'react'
import { api } from '../lib/api'
import { bytes } from '../lib/format'
import { displayAddress, subnetStatus } from '../lib/cidr'
import type {
  NetworkDetail, NetworkFamily, NetworkRequest, NetworkSummary, SubnetInUse,
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

  function submit(event: FormEvent) {
    event.preventDefault()
    let config: Record<string, string>
    try {
      config = desired()
    } catch (cause) {
      setFormError((cause as Error).message)
      return
    }
    if (!network) {
      onSubmit({
        name: name.trim(),
        description: description.trim(),
        config: Object.fromEntries(Object.entries(config).filter(([, value]) => value !== '')),
      })
      return
    }
    // Only what changed: re-sending keys the daemon has but lemondx does not
    // allow (set with the daemon's own CLI, say) would be refused.
    const changes = Object.fromEntries(Object.entries(config).filter(([key, value]) =>
      value !== (current[key] ?? IMPLICIT[key] ?? '')))
    onSubmit({ description: description.trim(), config: changes })
  }

  const canSubmit = !busy && (network || name.trim())
    && !v4Status.error && !v6Status.error

  return (
    <Modal
      title={network ? `Edit ${network.name}` : 'Create network'}
      subtitle={network ? undefined
        : 'A managed bridge: the daemon runs DHCP and DNS on it and can NAT it behind the host.'}
      onClose={busy ? () => {} : onCancel}
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
