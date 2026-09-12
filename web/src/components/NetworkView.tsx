import { useCallback, useEffect, useState } from 'react'
import { api } from '../lib/api'
import { bytes } from '../lib/format'
import type { NetworkDetail, NetworkFamily, NetworkSummary } from '../lib/types'

/** How the daemon's networking is actually wired up. */
export function NetworkView() {
  const [summaries, setSummaries] = useState<NetworkSummary[] | null>(null)
  const [details, setDetails] = useState<Record<string, NetworkDetail>>({})
  const [error, setError] = useState<string | null>(null)

  const load = useCallback((signal?: AbortSignal) => {
    api.networks(signal)
      .then(async (list) => {
        setSummaries(list)
        setError(null)
        // Only managed networks have config worth expanding.
        const managed = list.filter((n) => n.managed)
        const loaded = await Promise.all(
          managed.map((n) => api.network(n.name, signal).catch(() => null)))
        const next: Record<string, NetworkDetail> = {}
        loaded.forEach((detail) => { if (detail) next[detail.name] = detail })
        setDetails(next)
      })
      .catch((cause) => {
        if ((cause as Error).name !== 'AbortError') setError((cause as Error).message)
      })
  }, [])

  useEffect(() => {
    const controller = new AbortController()
    load(controller.signal)
    const timer = window.setInterval(() => load(), 5000)
    return () => { controller.abort(); window.clearInterval(timer) }
  }, [load])

  if (error) {
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
      {managed.length === 0 && (
        <div className="card">
          <div className="empty">
            <h3>No managed networks</h3>
            <p>Run setup to create a bridge for your containers.</p>
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
              {detail && (
                <span className="faint net-traffic num">
                  ↓ {bytes(detail.counters.rx)} · ↑ {bytes(detail.counters.tx)}
                </span>
              )}
            </header>

            {!detail ? (
              <div className="loading-wrap"><span className="spinner" /></div>
            ) : (
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
                        <span className="mono">
                          {detail.ipv4.address.split('/')[0]}
                        </span>{' '}
                        as their gateway and DNS resolver
                        {detail.dns_domain && (
                          <> in the <span className="mono">{detail.dns_domain}</span> domain</>
                        )}
                        .{' '}
                        {detail.ipv4.nat
                          ? 'Outbound traffic is NATed behind the host, so containers can reach the internet but are not reachable from outside.'
                          : 'NAT is off, so outbound traffic needs a route on your network.'}
                      </>
                    ) : (
                      <>This network hands out no IPv4 addresses.</>
                    )}
                  </p>
                  {(detail.profiles.length > 0 || detail.instances.length > 0) && (
                    <p className="net-explain">
                      Attached via{' '}
                      {detail.profiles.length > 0 && (
                        <>profile{detail.profiles.length > 1 ? 's' : ''}{' '}
                          <span className="mono">{detail.profiles.join(', ')}</span></>
                      )}
                      {detail.profiles.length > 0 && detail.instances.length > 0 && ', plus '}
                      {detail.instances.length > 0 && (
                        <>directly by <span className="mono">{detail.instances.join(', ')}</span></>
                      )}
                      .
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
                <tr><th>Name</th><th>Type</th><th>Used by</th></tr>
              </thead>
              <tbody>
                {others.map((network) => (
                  <tr key={network.name}>
                    <td className="mono">{network.name}</td>
                    <td className="dim">{network.type}</td>
                    <td className="dim">{network.used_by || '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="hint" style={{ padding: '0 14px 12px' }}>
            These exist on the host but the daemon does not configure them. A
            container can still be attached to one with a bridged NIC device.
          </p>
        </section>
      )}
    </>
  )
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
