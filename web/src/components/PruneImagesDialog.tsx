import { useEffect, useState } from 'react'
import { api, calls } from '../lib/api'
import { bytes } from '../lib/format'
import type { PruneResult } from '../lib/types'
import { Modal } from './Modal'

interface Props {
  onClose: () => void
  onDone: (result: PruneResult) => void
}

/**
 * Delete downloaded images nothing needs, on every node. Asks each node first
 * and shows its answer; confirming deletes exactly what was shown, so an image
 * a launch started using meanwhile is kept rather than pulled from under it.
 */
export function PruneImagesDialog({ onClose, onDone }: Props) {
  const [preview, setPreview] = useState<PruneResult | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [showKept, setShowKept] = useState(false)

  useEffect(() => {
    let live = true
    api.pruneImages({}).then((result) => { if (live) setPreview(result) })
      .catch((cause) => { if (live) setError((cause as Error).message) })
    return () => { live = false }
  }, [])

  const only = Object.fromEntries((preview?.nodes ?? []).map(
    (n) => [n.node, n.deleted.map((i) => i.fingerprint)]))
  const count = (preview?.nodes ?? []).reduce((sum, n) => sum + n.deleted.length, 0)

  async function prune() {
    setBusy(true)
    setError(null)
    try {
      onDone(await api.pruneImages({ apply: true, only }))
    } catch (cause) {
      setError((cause as Error).message)
      setBusy(false)
    }
  }

  return (
    <Modal title="Prune images"
      subtitle="Deletes images downloaded from a remote that no instance was made from, no pin holds and no template launches. Images made here from snapshots are never touched."
      onClose={busy ? () => {} : onClose}
      api={calls.pruneImages({ apply: true, only })}
      footer={<>
        <button type="button" className="btn" onClick={onClose} disabled={busy}>Cancel</button>
        <button type="button" className="btn btn-danger" onClick={prune}
          disabled={busy || !preview || count === 0}>
          {busy && <span className="spinner" />}
          {count ? `Delete ${count} image${count === 1 ? '' : 's'} (${bytes(preview?.freed ?? 0)})`
            : 'Nothing to delete'}
        </button>
      </>}>
      {!preview && !error && (
        <div className="loading-wrap"><span className="spinner" /> Asking every node…</div>
      )}
      {preview && (
        <div style={{ display: 'grid', gap: 12 }}>
          {preview.nodes.map((node) => (
            <div key={node.node}>
              <strong>{node.node}</strong>
              <span className="faint">
                {node.error ? '' : node.deleted.length
                  ? ` · ${node.deleted.length} to delete, ${bytes(node.freed)}`
                  : ' · nothing to delete'}
              </span>
              {node.error && <div className="field-error">{node.error}</div>}
              {node.deleted.map((image) => (
                <div key={image.fingerprint} className="faint" style={{ fontSize: 12 }}>
                  <span className="mono">{image.fingerprint.slice(0, 12)}</span>{' '}
                  {image.description || image.aliases.join(', ') || 'no name'}
                  {image.type === 'virtual-machine' ? ' · VM' : ''} · {bytes(image.size)}
                </div>
              ))}
              {showKept && node.kept.map((image) => (
                <div key={image.fingerprint} className="faint" style={{ fontSize: 12, opacity: 0.75 }}>
                  kept <span className="mono">{image.fingerprint.slice(0, 12)}</span>{' '}
                  {image.description || image.aliases.join(', ')} — {image.reason}
                </div>
              ))}
            </div>
          ))}
          <label className="checkbox" style={{ fontSize: 12.5 }}>
            <input type="checkbox" checked={showKept} onChange={(e) => setShowKept(e.target.checked)} />
            Show what is kept, and why
          </label>
        </div>
      )}
      {error && <span className="field-error" role="alert">{error}</span>}
    </Modal>
  )
}
