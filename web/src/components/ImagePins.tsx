import { useMemo, useState } from 'react'
import { api, calls } from '../lib/api'
import { bytes, relativeTime } from '../lib/format'
import { entryFor, pinRef } from '../lib/pins'
import type { ImageBrowse, ImageJob, ImagePin, NodeImage, RemoteImage } from '../lib/types'
import { ConfirmDialog } from './ConfirmDialog'
import { Modal } from './Modal'

// Enough to scan; a search that matches more wants another word.
const MAX_RESULTS = 40

const SEARCHED = ['full_alias', 'os', 'release', 'release_title', 'label', 'variant'] as const

/** Every word somewhere in the entry, so "debian 12" and "12 debian" both find it. */
function matches(image: RemoteImage, words: string[]) {
  return words.every((word) => SEARCHED.some((key) => String(image[key]).toLowerCase().includes(word)))
}

interface SearchProps {
  browse: ImageBrowse | null
  browseError: string | null
  canWrite: boolean
  onPin: (image: RemoteImage) => void
}

/** Search the remotes' catalogs, and pin what is found to one of its builds. */
export function RemoteSearch({ browse, browseError, canWrite, onPin }: SearchProps) {
  const [query, setQuery] = useState('')
  const [remote, setRemote] = useState('')
  const words = query.toLowerCase().split(/\s+/).filter(Boolean)
  const found = useMemo(() => {
    const wanted = query.toLowerCase().split(/\s+/).filter(Boolean)
    return browse && wanted.length
      ? browse.entries.filter((e) => (!remote || e.remote === remote) && matches(e, wanted))
      : []
  }, [browse, remote, query])

  return (
    <div className="panel">
      <h3>Search remotes</h3>
      <div className="card" style={{ padding: 12, display: 'grid', gap: 10 }}>
        <div className="browser-controls">
          <input className="input" value={query} autoComplete="off" aria-label="Search remote images"
            placeholder="debian 12, ubuntu 24.04, alpine…" onChange={(e) => setQuery(e.target.value)} />
          {browse && browse.browsed.length > 1 && (
            <select className="select browser-remote" value={remote} aria-label="Remote"
              onChange={(e) => setRemote(e.target.value)}>
              <option value="">All remotes</option>
              {browse.browsed.map((name) => <option key={name} value={name}>{name}:</option>)}
            </select>
          )}
        </div>
        {browseError && <span className="field-error">Could not read the catalogs: {browseError}</span>}
        {browse && Object.entries(browse.errors).map(([name, message]) => (
          <span key={name} className="hint" style={{ color: 'var(--warn)' }}>{name}: {message}</span>
        ))}
        {!browse && !browseError && (
          <div className="loading-wrap"><span className="spinner" /> Reading catalogs…</div>
        )}
        {browse && !words.length && (
          <span className="hint">
            {browse.entries.length} images on {browse.browsed.map((r) => `${r}:`).join(', ')} for{' '}
            {browse.architecture}. Pinning keeps one build of it, which templates launch as
            {' '}<span className="mono">pin:&lt;name&gt;</span>: the same base next month as
            today, instead of whatever the remote built last.
          </span>
        )}
        {browse && words.length > 0 && found.length === 0 && (
          <span className="hint">Nothing matches “{query}”.</span>
        )}
        {found.length > 0 && (
          <div className="table-scroll">
            <table className="ctable">
              <thead>
                <tr><th>Image</th><th>Newest build</th><th>Size</th><th aria-label="Actions" /></tr>
              </thead>
              <tbody>
                {found.slice(0, MAX_RESULTS).map((image) => (
                  <tr key={`${image.remote}/${image.alias}/${image.arch}`}>
                    <td>
                      <strong>{image.label}</strong>
                      {image.variant !== 'default' && <span className="faint"> · {image.variant}</span>}
                      <div className="res-sub mono">{image.full_alias}</div>
                    </td>
                    <td className="mono">
                      {image.serial}
                      {image.versions.length > 1 && (
                        <div className="res-sub">{image.versions.length} builds on offer</div>
                      )}
                    </td>
                    <td className="num">{bytes(image.size || image.vm_size)}</td>
                    <td style={{ whiteSpace: 'nowrap', textAlign: 'right' }}>
                      {image.pins.length > 0 && (
                        <span className="badge" style={{ marginRight: 6 }}
                          title={image.pins.map((p) => p.serial).join(', ')}>
                          {image.pins.length} pinned
                        </span>
                      )}
                      <button className="btn btn-sm" disabled={!canWrite} onClick={() => onPin(image)}>
                        Pin a build…
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            {found.length > MAX_RESULTS && (
              <span className="hint">{found.length - MAX_RESULTS} more — add a word to narrow it.</span>
            )}
          </div>
        )}
      </div>
    </div>
  )
}

interface PinsProps {
  pins: ImagePin[] | null
  browse: ImageBrowse | null
  images: NodeImage[]
  nodes: string[]
  localNode: string
  canWrite: boolean
  onJobStarted: (job: ImageJob, node?: string) => void
  onNotify: (kind: 'success' | 'error', title: string, detail?: string) => void
  /** A pin was edited or removed. */
  onChanged: () => void
}

/** Every pinned build, its names, what launches it, and which nodes have it. */
export function PinnedImages({
  pins, browse, images, nodes, localNode, canWrite, onJobStarted, onNotify, onChanged,
}: PinsProps) {
  const [unpinning, setUnpinning] = useState<ImagePin | null>(null)
  const [editing, setEditing] = useState<ImagePin | null>(null)
  const [busy, setBusy] = useState(false)
  if (!pins || pins.length === 0) return null
  const clustered = nodes.length > 1

  function holders(pin: ImagePin) {
    const wanted = new Set([pin.fingerprint, pin.vm_fingerprint].filter(Boolean))
    return new Set(images.filter((i) => wanted.has(i.fingerprint)).map((i) => i.node))
  }

  async function fetchMissing(pin: ImagePin, missing: string[], from: string) {
    try {
      const job = await api.fetchPin(pin.name, missing, from === localNode ? undefined : from)
      onJobStarted(job, from === localNode ? undefined : from)
    } catch (cause) {
      onNotify('error', `Could not fetch ${pin.name}`, (cause as Error).message)
    }
  }

  async function unpin() {
    if (!unpinning) return
    setBusy(true)
    try {
      const done = await api.unpinImage(unpinning.name)
      onNotify('success', `Unpinned ${done.unpinned}`,
        `The build stays on each node as local:${done.kept}.`)
      onChanged()
    } catch (cause) {
      onNotify('error', `Could not unpin ${unpinning.name}`, (cause as Error).message)
    } finally {
      setBusy(false)
      setUnpinning(null)
    }
  }

  return (
    <div className="panel">
      <h3>Pinned builds</h3>
      <div className="card table-scroll">
        <table className="ctable">
          <thead>
            <tr>
              <th>Pin</th><th>Build</th>
              {clustered && <th>Nodes</th>}
              <th aria-label="Actions" />
            </tr>
          </thead>
          <tbody>
            {pins.map((pin) => {
              const held = holders(pin)
              const missing = nodes.filter((n) => !held.has(n))
              // A fetch runs on a node that has the build and sends it on.
              const from = held.has(localNode) ? localNode : [...held][0]
              const newest = entryFor(pin, browse)?.serial
              return (
                <tr key={pin.name}>
                  <td>
                    <strong className="mono">{pinRef(pin)}</strong>
                    <div className="res-sub">
                      {pin.label} · <span className="mono">{pin.image}</span>
                      {pin.note ? ` — ${pin.note}` : ''}
                    </div>
                    <div className="res-sub mono">
                      {[pin.name, ...pin.nicknames].filter((n) => `pin:${n}` !== pinRef(pin))
                        .map((n) => `pin:${n}`).join(' · ')}
                    </div>
                    {pin.used_by.length > 0 && (
                      <div className="res-sub">launched by {pin.used_by.join(', ')}</div>
                    )}
                  </td>
                  <td>
                    <span className="mono">{pin.serial}</span>
                    <div className="res-sub mono" title={pin.fingerprint || pin.vm_fingerprint}>
                      {(pin.fingerprint || pin.vm_fingerprint).slice(0, 12)}
                      {pin.pinned_at ? ` · ${relativeTime(new Date(pin.pinned_at * 1000).toISOString())}` : ''}
                      {pin.pinned_by ? ` by ${pin.pinned_by}` : ''}
                    </div>
                    {newest && newest !== pin.serial
                      && !pins.some((p) => p.image === pin.image && p.serial === newest) && (
                      <div className="res-sub">newer build on the remote: {newest}</div>
                    )}
                  </td>
                  {clustered && (
                    <td>
                      <div style={{ display: 'flex', gap: 4, flexWrap: 'wrap' }}>
                        {nodes.map((node) => (
                          <span key={node}
                            className={`badge ${held.has(node) ? 'badge-ok' : 'badge-dim'}`}
                            title={held.has(node) ? `${node} has this build`
                              : `${node} does not have it yet, and would fetch it at launch while the remote still serves it`}
                            style={held.has(node) ? undefined : { textDecoration: 'line-through' }}>
                            {node}
                          </span>
                        ))}
                      </div>
                    </td>
                  )}
                  <td style={{ whiteSpace: 'nowrap', textAlign: 'right' }}>
                    {missing.length > 0 && (
                      <button className="btn btn-sm" style={{ marginRight: 6 }}
                        disabled={!canWrite || !from}
                        title={from ? `Send it from ${from} to ${missing.join(', ')}`
                          : 'No node has this build yet'}
                        onClick={() => fetchMissing(pin, missing, from)}>
                        Fetch on {missing.length === nodes.length ? 'every node' : missing.join(', ')}
                      </button>
                    )}
                    <button className="btn btn-sm" style={{ marginRight: 6 }} disabled={!canWrite}
                      onClick={() => setEditing(pin)}>
                      Nicknames…
                    </button>
                    <button className="btn btn-sm btn-danger"
                      disabled={!canWrite || pin.used_by.length > 0}
                      title={pin.used_by.length ? `Launched by ${pin.used_by.join(', ')}: point ${pin.used_by.length > 1 ? 'them' : 'it'} at another image first` : undefined}
                      onClick={() => setUnpinning(pin)}>
                      Unpin…
                    </button>
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
      {unpinning && (
        <ConfirmDialog title={`Unpin ${pinRef(unpinning)}`} confirmLabel="Unpin" danger busy={busy}
          message={`Every node forgets this pin, and nothing can launch it by name any more. The build stays on each node as local:${unpinning.local_alias}, for you to delete when nothing needs it. Instances already running are not touched.`}
          api={{ method: 'DELETE', path: `/images/pins/${encodeURIComponent(unpinning.name)}` }}
          onConfirm={unpin} onCancel={() => setUnpinning(null)} />
      )}
      {editing && (
        <NicknameDialog pin={editing} onCancel={() => setEditing(null)}
          onSaved={() => { setEditing(null); onChanged() }} />
      )}
    </div>
  )
}

/** Nicknames as typed: separated by spaces or commas. */
function parseNames(text: string) {
  return [...new Set(text.split(/[\s,]+/).map((n) => n.trim()).filter(Boolean))]
}

interface DialogProps {
  image: RemoteImage
  /** The pins this image has already, so their builds are not offered twice. */
  pins: ImagePin[]
  nodes: string[]
  localNode: string
  onCancel: () => void
  onStarted: (job: ImageJob, pin: ImagePin) => void
}

/** Pin one build of an image for every node, and fetch it onto the nodes. */
export function PinDialog({ image, pins, nodes, localNode, onCancel, onStarted }: DialogProps) {
  const pinnedSerials = new Map(pins.map((p) => [p.serial, p]))
  const [serial, setSerial] = useState(
    image.versions.find((v) => !pinnedSerials.has(v.serial))?.serial ?? '')
  const [nicknames, setNicknames] = useState('')
  const [note, setNote] = useState('')
  const [chosen, setChosen] = useState<string[]>(nodes)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const body = { image: image.full_alias, serial, nicknames: parseNames(nicknames),
    note: note.trim(), nodes: nodes.length > 1 ? chosen : undefined }

  async function submit(event: React.FormEvent) {
    event.preventDefault()
    if (!serial || busy) return
    setBusy(true)
    setError(null)
    try {
      const result = await api.pinImage(body)
      onStarted(result.job, result.pin)
    } catch (cause) {
      setError((cause as Error).message)
      setBusy(false)
    }
  }

  return (
    <Modal title={`Pin a build of ${image.full_alias}`}
      subtitle="A pin is one build, for good: templates that name it launch exactly that base on every node, and a newer build is another pin beside it."
      onClose={busy ? () => {} : onCancel}
      api={calls.pinImage(body)}
      footer={<>
        <button type="button" className="btn" onClick={onCancel} disabled={busy}>Cancel</button>
        <button type="submit" form="pin-form" className="btn btn-primary"
          disabled={!serial || busy || (nodes.length > 1 && chosen.length === 0)}>
          {busy && <span className="spinner" />}Pin and fetch
        </button>
      </>}>
      <form id="pin-form" onSubmit={submit} style={{ display: 'grid', gap: 14 }}>
        <div className="field">
          <label>Build</label>
          <div className="check-list" style={{ maxHeight: 'none' }}>
            {image.versions.map((version, index) => {
              const pinned = pinnedSerials.get(version.serial)
              return (
                <label key={version.serial} className="check" style={{ alignItems: 'flex-start' }}>
                  <input type="radio" name="pin-build" checked={serial === version.serial}
                    disabled={busy || !!pinned} onChange={() => setSerial(version.serial)} />
                  <span>
                    <strong className="mono">{version.serial}</strong>
                    <span className="faint">
                      {index === 0 ? ' · newest' : ''}
                      {pinned ? ` · pinned as ${pinRef(pinned)}` : ''}
                    </span>
                    <div className="faint mono" style={{ fontSize: 11 }}>
                      {(version.container_fingerprint || version.vm_fingerprint || '').slice(0, 12)}
                      {' · '}{bytes(version.size || version.vm_size)}
                    </div>
                  </span>
                </label>
              )
            })}
          </div>
          <span className="hint">
            {serial ? 'A remote keeps only its last few builds, which is why pinning fetches the build at once.'
              : 'Every build the remote still serves is pinned already.'}
          </span>
        </div>
        <div className="field">
          <label htmlFor="pin-nicknames">Nicknames</label>
          <input id="pin-nicknames" className="input mono" value={nicknames} disabled={busy}
            autoComplete="off" placeholder="debian-golden" onChange={(e) => setNicknames(e.target.value)} />
          <span className="hint">
            Optional, separated by spaces: other names a template can launch it by, as{' '}
            <span className="mono">pin:{parseNames(nicknames)[0] ?? 'debian-golden'}</span>. Its id is
            made from the image and the build.
          </span>
        </div>
        {nodes.length > 1 && (
          <div className="field">
            <label>Fetch it on</label>
            <div className="check-list">
              {nodes.map((node) => (
                <label key={node} className="check">
                  <input type="checkbox" disabled={busy} checked={chosen.includes(node)}
                    onChange={() => setChosen(chosen.includes(node)
                      ? chosen.filter((n) => n !== node) : [...chosen, node])} />
                  <span>{node}{node === localNode ? ' (this node)' : ''}</span>
                </label>
              ))}
            </div>
            <span className="hint">
              Downloaded here, then sent to the others, so every copy is the same bytes —
              in a mix of LXD and Incus nodes there is no one remote they could all fetch it
              from. The pin itself applies on every node either way.
            </span>
          </div>
        )}
        <div className="field">
          <label htmlFor="pin-note">Why</label>
          <input id="pin-note" className="input" value={note} maxLength={200} disabled={busy}
            placeholder="certified base for the March release" onChange={(e) => setNote(e.target.value)} />
        </div>
        {error && <span className="field-error" role="alert">{error}</span>}
      </form>
    </Modal>
  )
}

/** A pin's nicknames and note: the only things about it that change. */
function NicknameDialog({ pin, onCancel, onSaved }: {
  pin: ImagePin
  onCancel: () => void
  onSaved: () => void
}) {
  const [nicknames, setNicknames] = useState(pin.nicknames.join(' '))
  const [note, setNote] = useState(pin.note)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const body = { nicknames: parseNames(nicknames), note: note.trim() }

  async function submit(event: React.FormEvent) {
    event.preventDefault()
    if (busy) return
    setBusy(true)
    setError(null)
    try {
      await api.editPin(pin.name, body)
      onSaved()
    } catch (cause) {
      setError((cause as Error).message)
      setBusy(false)
    }
  }

  return (
    <Modal title={`Nicknames for pin:${pin.name}`}
      subtitle={`Build ${pin.serial} of ${pin.image}. The build never changes; these are other names templates can launch it by.`}
      onClose={busy ? () => {} : onCancel}
      api={{ method: 'PATCH', path: `/images/pins/${encodeURIComponent(pin.name)}`, body }}
      footer={<>
        <button type="button" className="btn" onClick={onCancel} disabled={busy}>Cancel</button>
        <button type="submit" form="pin-nick-form" className="btn btn-primary" disabled={busy}>
          {busy && <span className="spinner" />}Save
        </button>
      </>}>
      <form id="pin-nick-form" onSubmit={submit} style={{ display: 'grid', gap: 14 }}>
        <div className="field">
          <label htmlFor="pin-nick">Nicknames</label>
          <input id="pin-nick" className="input mono" value={nicknames} disabled={busy}
            autoComplete="off" placeholder="debian-golden" onChange={(e) => setNicknames(e.target.value)} />
          <span className="hint">
            Separated by spaces; lower-case letters, digits and . _ -. Each names one pin
            across the cluster, and one a template launches by cannot be taken off
            {pin.used_by.length ? ` (launched by ${pin.used_by.join(', ')})` : ''}.
          </span>
        </div>
        <div className="field">
          <label htmlFor="pin-nick-note">Why</label>
          <input id="pin-nick-note" className="input" value={note} maxLength={200} disabled={busy}
            onChange={(e) => setNote(e.target.value)} />
        </div>
        {error && <span className="field-error" role="alert">{error}</span>}
      </form>
    </Modal>
  )
}
