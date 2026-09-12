import { useEffect, useState } from 'react'
import { api } from '../lib/api'
import type { BootstrapSelection, CreateRequest, Images, Status } from '../lib/types'
import { useBootstrapData } from '../hooks/useBootstrapData'
import { BootstrapPicker } from './BootstrapPicker'
import { ImageBrowser } from './ImageBrowser'
import { SshKeyPicker } from './SshKeyPicker'
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
  const { modules, hostKeys } = useBootstrapData()
  const [bootstrap, setBootstrap] = useState<BootstrapSelection>(
    { modules: [], params: {}, ssh_keys: [] })

  // Fingerprints of images already on this host, so the quick-pick grid can
  // mark them. Best effort: the grid still works if the catalog is unreachable.
  const [cachedAliases, setCachedAliases] = useState<Set<string>>(new Set())

  useEffect(() => {
    const controller = new AbortController()
    api.images(controller.signal).then(setImages).catch(() => {})
    api.status(controller.signal).then(setStatus).catch(() => {})
    api.browseImages({}, controller.signal)
      .then((browse) => setCachedAliases(
        new Set(browse.entries.filter((e) => e.cached).map((e) => e.full_alias))))
      .catch(() => {})
    return () => controller.abort()
  }, [])

  // Some drivers (notably dir, without filesystem project quotas) accept a
  // disk size and then ignore it. The server tells us which pool a container
  // lands on and whether it can enforce one.
  const rootPool = status?.root_pool ?? null
  const quotaless = rootPool !== null && !rootPool.supports_quota

  const nameValid = NAME_RULE.test(name)

  // Modules that install keys make the key picker a first-class part of the
  // dialog, and a required one -- creating without keys would just fail.
  const needsKeys = modules.some(
    (m) => m.uses_ssh_keys && bootstrap.modules.includes(m.id))
  const keysMissing = needsKeys && bootstrap.ssh_keys.length === 0

  // Keys without a listening sshd is a dead end, so say so -- softly, since
  // exec-only containers and externally-managed sshd are both legitimate.
  const SSH_SERVER = 'ssh-server'
  const hasSshServerModule = modules.some((m) => m.id === SSH_SERVER)
  const noSshServer = needsKeys && hasSshServerModule
    && !bootstrap.modules.includes(SSH_SERVER)

  const canSubmit = nameValid && image.trim().length > 0 && !busy && !keysMissing

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
        bootstrap: bootstrap.modules.length > 0 ? bootstrap : undefined,
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
                    {cachedAliases.has(choice.alias) && (
                      <span className="badge badge-ok choice-cached">Downloaded</span>
                    )}
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
                Browse all images…
              </button>
            </>
          ) : (
            <ImageBrowser
              value={image}
              forVm={isVm}
              disabled={busy}
              onPick={setImage}
              onBack={() => setCustomImage(false)}
            />
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
          Leave blank for no limit. CPU is a core count; memory and disk take a
          unit (<span className="mono">4GiB</span>, <span className="mono">512MiB</span>)
          — a bare number is read as GiB.
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
            <input type="checkbox" checked={start}
              disabled={busy || bootstrap.modules.length > 0}
              onChange={(e) => setStart(e.target.checked)} />
            Start immediately after creating
            {bootstrap.modules.length > 0 && (
              <span className="hint">— required by the selected modules</span>
            )}
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

        <details className="bootstrap-section" open={bootstrap.modules.length > 0}>
          <summary>
            Bootstrap
            {bootstrap.modules.length > 0 && (
              <span className="badge badge-info">{bootstrap.modules.length} selected</span>
            )}
          </summary>
          <p className="hint" style={{ marginBottom: 10 }}>
            Bash modules run inside the container once it is up, in order.
          </p>
          <BootstrapPicker
            modules={modules}
            value={bootstrap}
            onChange={setBootstrap}
            disabled={busy}
          />
        </details>

        {needsKeys && (
          <SshKeyPicker
            hostKeys={hostKeys}
            value={bootstrap.ssh_keys}
            onChange={(keys) => setBootstrap({ ...bootstrap, ssh_keys: keys })}
            disabled={busy}
          />
        )}

        {noSshServer && !keysMissing && (
          <span className="hint" style={{ marginTop: -8 }}>
            Keys will be installed, but nothing will be listening — add the{' '}
            <strong>SSH server</strong> module if you want to ssh in.
          </span>
        )}

        {keysMissing && (
          <span className="hint" style={{ color: 'var(--warn)', marginTop: -8 }}>
            Select at least one key — {modules
              .filter((m) => m.uses_ssh_keys && bootstrap.modules.includes(m.id))
              .map((m) => m.name)
              .join(' and ')}{' '}
            installs keys.
          </span>
        )}

        {busy && bootstrap.modules.length > 0 && (
          <p className="hint">
            Running {bootstrap.modules.length} module(s) — installing packages can
            take a minute.
          </p>
        )}

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
