import { useEffect, useState } from 'react'
import { api } from '../lib/api'
import type { Snapshot, SnapshotSource } from '../lib/types'

interface Props {
  value: SnapshotSource | null
  onChange: (next: SnapshotSource | null) => void
  disabled?: boolean
}

interface Candidate {
  node: string
  name: string
  snapshots: number
}

/**
 * Which snapshot a template clones: an instance on any node, then one of its
 * snapshots. Only instances that have a snapshot are offered, and the node
 * comes along with the choice, since the template's instances are made there.
 */
export function SnapshotPicker({ value, onChange, disabled }: Props) {
  const [candidates, setCandidates] = useState<Candidate[] | null>(null)
  // Kept with the instance they were listed for, so a reply for the previous
  // choice is never shown against the current one.
  const [listed, setListed] = useState<{ key: string; snapshots: Snapshot[] } | null>(null)
  const [error, setError] = useState('')

  useEffect(() => {
    const controller = new AbortController()
    api.clusterContainers({ all: true }, controller.signal)
      .then((listing) => setCandidates(listing.instances
        .filter((c) => c.snapshot_count > 0)
        .map((c) => ({ node: c.node, name: c.name, snapshots: c.snapshot_count }))
        .sort((a, b) => a.node.localeCompare(b.node) || a.name.localeCompare(b.name))))
      .catch((cause) => {
        if ((cause as Error).name !== 'AbortError') setError((cause as Error).message)
      })
    return () => controller.abort()
  }, [])

  const node = value?.node ?? ''
  const instance = value?.instance ?? ''
  const chosenKey = instance ? `${node}\u0000${instance}` : ''
  useEffect(() => {
    if (!instance) return
    // Always through the node, even this one: the proxy answers a call naming
    // this node locally, so one path covers both.
    api.snapshots(instance, node || undefined)
      .then((snapshots) => setListed({ key: `${node}\u0000${instance}`, snapshots }))
      .catch((cause) => setError((cause as Error).message))
  }, [node, instance])
  const snapshots = listed && listed.key === chosenKey ? listed.snapshots : null

  const nodes = [...new Set((candidates ?? []).map((c) => c.node))]
  const known = (candidates ?? []).some((c) => c.node === node && c.name === instance)

  return (
    <>
      <div className="grid-2">
        <div className="field">
          <label htmlFor="t-snap-instance">Instance</label>
          <select id="t-snap-instance" className="select" value={chosenKey}
            disabled={disabled || candidates === null}
            onChange={(event) => {
              const [nextNode, nextInstance] = event.target.value.split('\u0000')
              onChange(nextInstance ? { node: nextNode, instance: nextInstance, name: '' } : null)
            }}>
            <option value="">
              {candidates === null ? 'Loading…'
                : candidates.length === 0 ? 'No instance has a snapshot' : 'Choose an instance…'}
            </option>
            {instance && !known && (
              <option value={chosenKey}>{instance}{node ? ` on ${node}` : ''} (not found)</option>
            )}
            {nodes.map((n) => (
              <optgroup key={n} label={n}>
                {(candidates ?? []).filter((c) => c.node === n).map((c) => (
                  <option key={c.name} value={`${c.node}\u0000${c.name}`}>
                    {c.name} ({c.snapshots} snapshot{c.snapshots === 1 ? '' : 's'})
                  </option>
                ))}
              </optgroup>
            ))}
          </select>
        </div>
        <div className="field">
          <label htmlFor="t-snap-name">Snapshot</label>
          <select id="t-snap-name" className="select" value={value?.name ?? ''}
            disabled={disabled || !instance || snapshots === null}
            onChange={(event) => value && onChange({ ...value, name: event.target.value })}>
            <option value="">{instance && snapshots === null ? 'Loading…' : 'Choose a snapshot…'}</option>
            {value?.name && snapshots && !snapshots.some((s) => s.name === value.name) && (
              <option value={value.name}>{value.name} (not found)</option>
            )}
            {(snapshots ?? []).map((s) => (
              <option key={s.name} value={s.name}>{s.name}</option>
            ))}
          </select>
        </div>
      </div>
      <span className="hint" style={{ marginTop: -8 }}>
        {error ? <span style={{ color: 'var(--danger)' }}>{error}</span> : (
          <>
            Instances are clones of the snapshot, made on
            {node ? <> <span className="mono">{node}</span></> : ' its node'} — the only
            place it exists. To launch it on other nodes, use <em>Make image</em> on the
            snapshot and template <span className="mono">local:&lt;alias&gt;</span> instead.
            Whether it is a container or a VM is the snapshot&apos;s.
          </>
        )}
      </span>
    </>
  )
}
