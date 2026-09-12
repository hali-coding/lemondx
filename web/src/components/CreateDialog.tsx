import { useEffect, useState } from 'react'
import { api } from '../lib/api'
import type { CreateRequest, Images, Status } from '../lib/types'
import { Modal } from './Modal'

interface Props {
  onCancel: () => void
  onCreate: (request: CreateRequest) => Promise<void>
}

const NAME_RULE = /^[a-zA-Z][a-zA-Z0-9-]{0,61}$/

export function CreateDialog({ onCancel, onCreate }: Props) {
  const [images, setImages] = useState<Images | null>(null)
  const [status, setStatus] = useState<Status | null>(null)
  const [name, setName] = useState('')
  const [image, setImage] = useState('ubuntu:24.04')
  const [customImage, setCustomImage] = useState(false)
  const [cpu, setCpu] = useState('')
  const [memory, setMemory] = useState('')
  const [disk, setDisk] = useState('')
  const [isVm, setIsVm] = useState(false)
  const [ephemeral, setEphemeral] = useState(false)
  const [start, setStart] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    const controller = new AbortController()
    api.images(controller.signal).then(setImages).catch(() => {})
    api.status(controller.signal).then(setStatus).catch(() => {})
    return () => controller.abort()
  }, [])

  // Some drivers (notably dir, without filesystem project quotas) accept a
  // disk size and then ignore it. The server tells us which pool a container
  // lands on and whether it can enforce one.
  const rootPool = status?.root_pool ?? null
  const quotaless = rootPool !== null && !rootPool.supports_quota

  const nameValid = NAME_RULE.test(name)
  const canSubmit = nameValid && image.trim().length > 0 && !busy

  async function submit(event: React.FormEvent) {
    event.preventDefault()
    if (!canSubmit) return
    setBusy(true)
    setError(null)
    try {
      await onCreate({
        name: name.trim(),
        image: image.trim(),
        type: isVm ? 'virtual-machine' : 'container',
        cpu: cpu.trim() || undefined,
        memory: memory.trim() || undefined,
        disk: disk.trim() || undefined,
        ephemeral,
        start,
      })
    } catch (cause) {
      setError((cause as Error).message)
      setBusy(false)
    }
  }

  return (
    <Modal
      title="New container"
      subtitle="The image is downloaded on first use, which can take a minute."
      onClose={busy ? () => {} : onCancel}
      footer={
        <>
          <button type="button" className="btn" onClick={onCancel} disabled={busy}>
            Cancel
          </button>
          <button type="submit" form="create-form" className="btn btn-primary" disabled={!canSubmit}>
            {busy && <span className="spinner" />}
            {busy ? 'Creating…' : 'Create'}
          </button>
        </>
      }
    >
      <form id="create-form" onSubmit={submit} style={{ display: 'contents' }}>
        <div className="field">
          <label htmlFor="c-name">Name</label>
          <input
            id="c-name"
            className="input"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="my-container"
            autoComplete="off"
            aria-invalid={name.length > 0 && !nameValid}
            disabled={busy}
          />
          {name.length > 0 && !nameValid && (
            <span className="hint" style={{ color: 'var(--danger)' }}>
              Start with a letter; letters, digits and dashes only.
            </span>
          )}
        </div>

        <div className="field">
          <label>Image</label>
          {!customImage ? (
            <>
              <div className="image-choices">
                {(images?.catalog ?? []).map((choice) => (
                  <button
                    type="button"
                    key={choice.alias}
                    className="image-choice"
                    aria-pressed={image === choice.alias}
                    onClick={() => setImage(choice.alias)}
                    disabled={busy}
                  >
                    <strong>{choice.label}</strong>
                    <span>{choice.alias}</span>
                  </button>
                ))}
              </div>
              <button
                type="button"
                className="btn btn-ghost btn-sm"
                style={{ alignSelf: 'flex-start', marginTop: 4 }}
                onClick={() => setCustomImage(true)}
                disabled={busy}
              >
                Use a different image…
              </button>
            </>
          ) : (
            <>
              <input
                className="input mono"
                value={image}
                onChange={(e) => setImage(e.target.value)}
                placeholder="images:debian/12"
                autoComplete="off"
                disabled={busy}
              />
              <span className="hint">
                Remotes: {(images?.remotes ?? ['ubuntu', 'images']).join(', ')}
              </span>
              <button
                type="button"
                className="btn btn-ghost btn-sm"
                style={{ alignSelf: 'flex-start' }}
                onClick={() => setCustomImage(false)}
                disabled={busy}
              >
                ← Back to the list
              </button>
            </>
          )}
        </div>

        <div className="grid-3">
          <div className="field">
            <label htmlFor="c-cpu">CPU limit</label>
            <input id="c-cpu" className="input" value={cpu} disabled={busy}
              onChange={(e) => setCpu(e.target.value)} placeholder="2" autoComplete="off" />
          </div>
          <div className="field">
            <label htmlFor="c-mem">Memory</label>
            <input id="c-mem" className="input" value={memory} disabled={busy}
              onChange={(e) => setMemory(e.target.value)} placeholder="2GiB" autoComplete="off" />
          </div>
          <div className="field">
            <label htmlFor="c-disk">Disk</label>
            <input id="c-disk" className="input" value={disk} disabled={busy}
              onChange={(e) => setDisk(e.target.value)} placeholder="10GiB" autoComplete="off" />
          </div>
        </div>
        <span className="hint" style={{ marginTop: -8 }}>
          Leave blank for no limit.
          {quotaless && disk.trim() && (
            <>
              {' '}
              <span style={{ color: 'var(--warn)' }}>
                Pool <span className="mono">{rootPool?.name}</span> uses the{' '}
                <span className="mono">{rootPool?.driver}</span> driver, which cannot
                enforce a disk size unless the filesystem has project quotas
                enabled — the value will be recorded but not applied.
              </span>
            </>
          )}
        </span>

        <div style={{ display: 'flex', flexDirection: 'column', gap: 9 }}>
          <label className="checkbox">
            <input type="checkbox" checked={start} disabled={busy}
              onChange={(e) => setStart(e.target.checked)} />
            Start immediately after creating
          </label>
          <label className="checkbox">
            <input type="checkbox" checked={isVm} disabled={busy}
              onChange={(e) => setIsVm(e.target.checked)} />
            Create a virtual machine instead of a container
          </label>
          <label className="checkbox">
            <input type="checkbox" checked={ephemeral} disabled={busy}
              onChange={(e) => setEphemeral(e.target.checked)} />
            Ephemeral — delete automatically when stopped
          </label>
        </div>

        {error && (
          <div className="banner banner-error" style={{ margin: 0, padding: '10px 12px' }}>
            <div className="banner-body">
              <p style={{ margin: 0, color: 'var(--danger)' }}>{error}</p>
            </div>
          </div>
        )}
      </form>
    </Modal>
  )
}
