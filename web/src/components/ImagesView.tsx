import { useCallback, useEffect, useMemo, useState } from 'react'
import { useCanWrite } from '../hooks/useAuth'
import { api } from '../lib/api'
import { absoluteTime, bytes, relativeTime } from '../lib/format'
import type {
  ImageBrowse, ImageInventory, ImageJob, ImagePin, NodeImage, NodeSnapshot, RemoteImage,
} from '../lib/types'
import { ImageJobRow } from './ImageJobRow'
import { pinRef } from '../lib/pins'
import { PinDialog, PinnedImages, RemoteSearch } from './ImagePins'
import { Modal } from './Modal'
import { PruneImagesDialog } from './PruneImagesDialog'
import { PublishDialog } from './PublishDialog'

interface Props {
  localNode: string
  /** Publishes and copies the page is following, on any node. */
  jobs: ImageJob[]
  /** A job started here, and the node it runs on when that is not this one. */
  onJobStarted: (job: ImageJob, node?: string) => void
  onDismissJob: (job: ImageJob) => void
  onNotify: (kind: 'success' | 'error', title: string, detail?: string) => void
}

/** One image across the cluster: the same fingerprint wherever it is. */
interface ClusterImage {
  fingerprint: string
  /** Every name any node knows it by. */
  aliases: string[]
  description: string
  size: number
  type: NodeImage['type']
  source: string | null
  /** Pulled from a remote on every node that has it. */
  cached: boolean
  /** The nodes holding it, each with its own copy's names. */
  copies: NodeImage[]
}

// Every probe asks each member for its images and instances, so the tab
// polls on its own, slower clock -- like the Cluster tab, for the same reason.
const POLL_MS = 10000

function group(images: NodeImage[]): ClusterImage[] {
  const byFingerprint = new Map<string, ClusterImage>()
  for (const image of images) {
    const known = byFingerprint.get(image.fingerprint)
    if (known) {
      known.copies.push(image)
      known.aliases = [...new Set([...known.aliases, ...image.aliases])].sort()
      known.cached = known.cached && image.cached
      known.description ||= image.description
      known.source ||= image.source
    } else {
      byFingerprint.set(image.fingerprint, {
        fingerprint: image.fingerprint, aliases: [...image.aliases],
        description: image.description, size: image.size, type: image.type,
        source: image.source, cached: image.cached, copies: [image],
      })
    }
  }
  return [...byFingerprint.values()].sort((a, b) =>
    Number(a.cached) - Number(b.cached)
    || (a.aliases[0] ?? '~').localeCompare(b.aliases[0] ?? '~'))
}

/**
 * Images and the snapshots they can be made from, across every node: where
 * each image is, where it is missing, and a way to copy it there. A template
 * launches one as `local:<alias>` on any node that holds it.
 */
export function ImagesView({ localNode, jobs, onJobStarted, onDismissJob, onNotify }: Props) {
  const canWrite = useCanWrite()
  const [inventory, setInventory] = useState<ImageInventory | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [showCached, setShowCached] = useState(false)
  const [copying, setCopying] = useState<ClusterImage | null>(null)
  const [deleting, setDeleting] = useState<ClusterImage | null>(null)
  const [publishing, setPublishing] = useState<NodeSnapshot | null>(null)
  const [pins, setPins] = useState<ImagePin[] | null>(null)
  const [browse, setBrowse] = useState<ImageBrowse | null>(null)
  const [browseError, setBrowseError] = useState<string | null>(null)
  const [pruning, setPruning] = useState(false)
  const [pinning, setPinning] = useState<RemoteImage | null>(null)

  const load = useCallback((signal?: AbortSignal) => {
    api.imageInventory(signal)
      .then((next) => { setInventory(next); setError(null) })
      .catch((cause) => {
        if ((cause as Error).name !== 'AbortError') setError((cause as Error).message)
      })
    api.imagePins(signal).then(setPins).catch(() => {})
  }, [])

  // The remotes' catalogs, once: the server caches them for a quarter of an
  // hour, and they change daily at most. A pin made here re-reads them, so
  // the "pinned" counts stay right.
  const loadCatalog = useCallback((signal?: AbortSignal) => {
    api.browseImages({}, signal).then((next) => { setBrowse(next); setBrowseError(null) })
      .catch((cause) => {
        if ((cause as Error).name !== 'AbortError') setBrowseError((cause as Error).message)
      })
  }, [])
  useEffect(() => {
    const controller = new AbortController()
    loadCatalog(controller.signal)
    return () => controller.abort()
  }, [loadCatalog])

  // A job finishing changes what is where, so it reloads rather than waiting
  // out the rest of the interval.
  const finished = jobs.filter((job) => job.finished_at !== null).length
  useEffect(() => {
    const controller = new AbortController()
    load(controller.signal)
    const timer = window.setInterval(() => load(controller.signal), POLL_MS)
    return () => {
      controller.abort()
      window.clearInterval(timer)
    }
  }, [load, finished])

  const images = useMemo(() => group(inventory?.images ?? []), [inventory])
  const shown = images.filter((image) => showCached || !image.cached)
  const hiddenCached = images.length - shown.length
  const nodes = inventory?.nodes ?? []
  const clustered = nodes.length > 1
  const remote = (node: string) => (node === localNode ? undefined : node)

  return (
    <>
      {error && (
        <div className="banner banner-error">
          <div className="banner-body"><p style={{ margin: 0 }}>{error}</p></div>
        </div>
      )}
      {(inventory?.errors ?? []).map((failure) => (
        <div key={failure.node} className="banner banner-error">
          <div className="banner-body">
            <p style={{ margin: 0 }}>
              <strong>{failure.node}</strong> did not answer, so its images and snapshots
              are missing here: {failure.error}
            </p>
          </div>
        </div>
      ))}

      {jobs.length > 0 && (
        <div className="panel">
          <h3>Publishing and copying</h3>
          <div className="card" style={{ padding: '4px 12px' }}>
            {jobs.map((job) => <ImageJobRow key={`${job.node}/${job.id}`} job={job}
              showNode={clustered} onDismiss={() => onDismissJob(job)} />)}
          </div>
        </div>
      )}

      <RemoteSearch browse={browse} browseError={browseError} canWrite={canWrite}
        onPin={setPinning} />

      <PinnedImages pins={pins} browse={browse} images={inventory?.images ?? []} nodes={nodes}
        localNode={localNode} canWrite={canWrite} onNotify={onNotify}
        onJobStarted={onJobStarted}
        onChanged={() => { load(); loadCatalog() }} />

      <div className="panel">
        <h3 style={{ display: 'flex', alignItems: 'center' }}>
          Images
          <button className="btn btn-sm" disabled={!canWrite} onClick={() => setPruning(true)}
            style={{ marginLeft: 'auto', textTransform: 'none', letterSpacing: 0 }}
            title="Delete downloaded images that nothing uses, pins or launches">
            Prune images…
          </button>
        </h3>
        {inventory === null ? (
          <div className="card"><div className="empty"><p>Loading…</p></div></div>
        ) : shown.length === 0 ? (
          <div className="card">
            <div className="empty">
              <h3>No images yet</h3>
              <p>Make one from a snapshot below; templates then launch it as local:&lt;name&gt;.</p>
            </div>
          </div>
        ) : (
          <div className="card table-scroll">
            <table className="ctable">
              <thead>
                <tr>
                  <th>Name</th><th>Made from</th><th>Size</th>
                  {clustered && <th>Nodes</th>}
                  <th aria-label="Actions" />
                </tr>
              </thead>
              <tbody>
                {shown.map((image) => {
                  const holders = new Set(image.copies.map((c) => c.node))
                  const missing = nodes.filter((n) => !holders.has(n))
                  const copyable = image.copies.some((c) => c.aliases.length > 0)
                  return (
                    <tr key={image.fingerprint}>
                      <td>
                        {image.aliases.length ? image.aliases.map((alias) => (
                          <div key={alias}><strong className="mono">local:{alias}</strong></div>
                        )) : <span className="faint">no name</span>}
                        <div className="res-sub mono" title={image.fingerprint}>
                          {image.fingerprint.slice(0, 12)}
                          {image.type === 'virtual-machine' ? ' · VM' : ''}
                          {image.cached ? ' · pulled from a remote' : ''}
                        </div>
                      </td>
                      <td>
                        {image.source
                          ? <>snapshot <span className="mono">{image.source}</span></>
                          : image.description || <span className="faint">—</span>}
                      </td>
                      <td className="num">{bytes(image.size)}</td>
                      {clustered && (
                        <td>
                          <div style={{ display: 'flex', gap: 4, flexWrap: 'wrap' }}>
                            {nodes.map((node) => (
                              <span key={node}
                                className={`badge ${holders.has(node) ? 'badge-ok' : 'badge-dim'}`}
                                title={holders.has(node) ? `On ${node}` : `Not on ${node}`}
                                style={holders.has(node) ? undefined : { textDecoration: 'line-through' }}>
                                {node}
                              </span>
                            ))}
                          </div>
                        </td>
                      )}
                      <td style={{ whiteSpace: 'nowrap', textAlign: 'right' }}>
                        {clustered && (
                          <button className="btn btn-sm" style={{ marginRight: 6 }}
                            disabled={!canWrite || !copyable || missing.length === 0}
                            title={!copyable ? 'Only a named image can be copied'
                              : missing.length === 0 ? 'Every node has it' : undefined}
                            onClick={() => setCopying(image)}>
                            Copy to…
                          </button>
                        )}
                        <button className="btn btn-sm btn-danger" disabled={!canWrite}
                          onClick={() => setDeleting(image)}>
                          Delete…
                        </button>
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}
        {hiddenCached > 0 || showCached ? (
          <label className="checkbox" style={{ marginTop: 8, fontSize: 12.5 }}>
            <input type="checkbox" checked={showCached}
              onChange={(e) => setShowCached(e.target.checked)} />
            Show images pulled from remotes{hiddenCached > 0 ? ` (${hiddenCached})` : ''} — the
            daemon caches these by itself when an instance is made from them
          </label>
        ) : null}
      </div>

      <div className="panel">
        <h3>Snapshots</h3>
        {inventory === null ? null : inventory.snapshots.length === 0 ? (
          <div className="card">
            <div className="empty">
              <h3>No snapshots</h3>
              <p>Take one from a container&apos;s Snapshots tab; it can then be made into an image here.</p>
            </div>
          </div>
        ) : (
          <div className="card table-scroll">
            <table className="ctable">
              <thead>
                <tr>
                  <th>Instance</th><th>Snapshot</th>{clustered && <th>Node</th>}<th>Taken</th>
                  <th aria-label="Actions" />
                </tr>
              </thead>
              <tbody>
                {inventory.snapshots.map((snapshot) => (
                  <tr key={`${snapshot.node}/${snapshot.instance}/${snapshot.name}`}>
                    <td>
                      <strong>{snapshot.instance}</strong>
                      {snapshot.type === 'virtual-machine' && <span className="res-sub"> · VM</span>}
                    </td>
                    <td className="mono">{snapshot.name}{snapshot.stateful ? ' (stateful)' : ''}</td>
                    {clustered && <td>{snapshot.node}</td>}
                    <td title={snapshot.created_at ? absoluteTime(snapshot.created_at) : undefined}>
                      {snapshot.created_at ? relativeTime(snapshot.created_at) : '—'}
                    </td>
                    <td style={{ textAlign: 'right' }}>
                      <button className="btn btn-sm" disabled={!canWrite}
                        onClick={() => setPublishing(snapshot)}>
                        Make image
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {pruning && (
        <PruneImagesDialog onClose={() => setPruning(false)}
          onDone={(result) => {
            setPruning(false)
            const deleted = result.nodes.reduce((sum, n) => sum + n.deleted.length, 0)
            onNotify(result.ok ? 'success' : 'error',
              `Pruned ${deleted} image${deleted === 1 ? '' : 's'}, ${bytes(result.freed)} freed`,
              result.nodes.filter((n) => n.error || n.failed.length)
                .map((n) => `${n.node}: ${n.error ?? n.failed.map((f) => f.error).join('; ')}`)
                .join(' ') || undefined)
            load()
          }} />
      )}
      {pinning && (
        <PinDialog image={pinning} nodes={nodes.length ? nodes : [localNode]}
          pins={(pins ?? []).filter((p) => p.remote === pinning.remote && p.arch === pinning.arch
            && p.aliases.includes(pinning.alias))}
          localNode={localNode} onCancel={() => setPinning(null)}
          onStarted={(job, pin) => {
            setPinning(null)
            onNotify('success', `Pinned build ${pin.serial} of ${pin.image}`,
              `Templates launch it as ${pinRef(pin)}. Fetching it now; progress is under Publishing and copying.`)
            onJobStarted(job)
            load()
            loadCatalog()
          }} />
      )}
      {publishing && (
        <PublishDialog instance={publishing.instance} snapshot={publishing.name}
          node={remote(publishing.node)}
          onCancel={() => setPublishing(null)}
          onStarted={(job) => {
            setPublishing(null)
            onJobStarted(job, remote(publishing.node))
          }} />
      )}
      {copying && (
        <CopyImageDialog image={copying} nodes={nodes} localNode={localNode}
          onCancel={() => setCopying(null)}
          onStarted={(job, node) => {
            setCopying(null)
            onJobStarted(job, remote(node))
          }} />
      )}
      {deleting && (
        <DeleteImageDialog image={deleting} localNode={localNode}
          onCancel={() => setDeleting(null)}
          onDone={(deleted, failures) => {
            setDeleting(null)
            if (deleted.length) {
              onNotify('success', `Deleted the image from ${deleted.join(', ')}`)
            }
            for (const failure of failures) {
              onNotify('error', `Could not delete it from ${failure.node}`, failure.error)
            }
            load()
          }} />
      )}
    </>
  )
}

function CopyImageDialog({ image, nodes, localNode, onCancel, onStarted }: {
  image: ClusterImage
  nodes: string[]
  localNode: string
  onCancel: () => void
  onStarted: (job: ImageJob, node: string) => void
}) {
  // Sent from a node that has it under a name: the copy takes that name.
  const sources = image.copies.filter((c) => c.aliases.length > 0)
  const [from, setFrom] = useState(
    (sources.find((c) => c.node === localNode) ?? sources[0]).node)
  const source = sources.find((c) => c.node === from) ?? sources[0]
  const holders = new Set(image.copies.map((c) => c.node))
  const missing = nodes.filter((n) => !holders.has(n))
  // Every node that lacks it, to start with: filling the gaps is what this is for.
  const [chosen, setChosen] = useState<string[]>(missing)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const alias = source.aliases[0]

  async function submit(event: React.FormEvent) {
    event.preventDefault()
    if (!chosen.length || busy) return
    setBusy(true)
    setError(null)
    try {
      onStarted(await api.copyImage(alias, chosen,
        source.node === localNode ? undefined : source.node), source.node)
    } catch (cause) {
      setError((cause as Error).message)
      setBusy(false)
    }
  }

  return (
    <Modal
      title={`Copy local:${alias}`}
      subtitle={`${bytes(image.size)} to each node chosen, sent from the one it is copied from.`}
      onClose={busy ? () => {} : onCancel}
      footer={
        <>
          <button type="button" className="btn" onClick={onCancel} disabled={busy}>Cancel</button>
          <button type="submit" form="copy-image-form" className="btn btn-primary"
            disabled={!chosen.length || busy}>
            {busy && <span className="spinner" />}
            Copy to {chosen.length || '…'}
          </button>
        </>
      }
    >
      <form id="copy-image-form" onSubmit={submit} style={{ display: 'contents' }}>
        {sources.length > 1 && (
          <div className="field">
            <label htmlFor="ci-from">Copy from</label>
            <select id="ci-from" className="select" value={from} disabled={busy}
              onChange={(e) => setFrom(e.target.value)}>
              {sources.map((c) => (
                <option key={c.node} value={c.node}>
                  {c.node}{c.node === localNode ? ' (this node)' : ''}
                </option>
              ))}
            </select>
          </div>
        )}
        <div className="field">
          <label>Copy to</label>
          <div className="check-list">
            {missing.map((node) => (
              <label key={node} className="check">
                <input type="checkbox" disabled={busy} checked={chosen.includes(node)}
                  onChange={() => setChosen(chosen.includes(node)
                    ? chosen.filter((n) => n !== node) : [...chosen, node])} />
                <span>{node}{node === localNode ? ' (this node)' : ''}</span>
              </label>
            ))}
          </div>
          <span className="hint">
            Named <span className="mono">{alias}</span> there too, replacing any image already
            called that. It is checked on arrival, and carries on if you close this page.
          </span>
        </div>
        {error && (
          <div className="banner banner-error" style={{ margin: 0, padding: '10px 12px' }}>
            <div className="banner-body"><p style={{ margin: 0, color: 'var(--danger)' }}>{error}</p></div>
          </div>
        )}
      </form>
    </Modal>
  )
}

function DeleteImageDialog({ image, localNode, onCancel, onDone }: {
  image: ClusterImage
  localNode: string
  onCancel: () => void
  onDone: (deleted: string[], failures: { node: string; error: string }[]) => void
}) {
  const holders = image.copies.map((c) => c.node)
  // Every node that holds it, to start with; untick the ones to keep it on.
  // The button says how many it will remove from before anything happens.
  const [chosen, setChosen] = useState<string[]>(holders)
  const [busy, setBusy] = useState(false)
  const name = image.aliases[0] ? `local:${image.aliases[0]}` : image.fingerprint.slice(0, 12)

  async function submit(event: React.FormEvent) {
    event.preventDefault()
    if (!chosen.length || busy) return
    setBusy(true)
    const deleted: string[] = []
    const failures: { node: string; error: string }[] = []
    for (const node of chosen) {
      try {
        await api.deleteImage(image.fingerprint, node === localNode ? undefined : node)
        deleted.push(node)
      } catch (cause) {
        failures.push({ node, error: (cause as Error).message })
      }
    }
    onDone(deleted, failures)
  }

  return (
    <Modal
      title={`Delete ${name}`}
      subtitle="Instances already made from it are not affected; templates that launch it block the delete."
      onClose={busy ? () => {} : onCancel}
      footer={
        <>
          <button type="button" className="btn" onClick={onCancel} disabled={busy}>Cancel</button>
          <button type="submit" form="delete-image-form" className="btn btn-danger"
            disabled={!chosen.length || busy}>
            {busy && <span className="spinner" />}
            Delete from {chosen.length || '…'}
          </button>
        </>
      }
    >
      <form id="delete-image-form" onSubmit={submit} style={{ display: 'contents' }}>
        <div className="field">
          <label>Delete from</label>
          <div className="check-list">
            {holders.map((node) => (
              <label key={node} className="check">
                <input type="checkbox" disabled={busy} checked={chosen.includes(node)}
                  onChange={() => setChosen(chosen.includes(node)
                    ? chosen.filter((n) => n !== node) : [...chosen, node])} />
                <span>{node}{node === localNode ? ' (this node)' : ''}</span>
              </label>
            ))}
          </div>
        </div>
      </form>
    </Modal>
  )
}
