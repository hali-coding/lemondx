import { useCallback, useEffect, useRef, useState } from 'react'
import { useCanWrite } from '../hooks/useAuth'
import type { FormEvent } from 'react'
import { api, calls } from '../lib/api'
import type { ApiCall } from '../lib/api'
import { bytes } from '../lib/format'
import type {
  StorageOverview, StoragePoolDetail, StoragePoolRequest, StorageVolume,
  StorageVolumeRequest,
} from '../lib/types'
import type { ToastKind } from '../hooks/useToasts'
import { ConfirmDialog } from './ConfirmDialog'
import { Modal } from './Modal'
import { PlusIcon } from './Icons'

interface Props {
  onNotify: (kind: ToastKind, title: string, detail?: string) => void
}

type PendingDelete =
  | { kind: 'pool'; pool: StoragePoolDetail }
  | { kind: 'volume'; volume: StorageVolume }

export function StorageView({ onNotify }: Props) {
  const canWrite = useCanWrite()
  const [data, setData] = useState<StorageOverview | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [tab, setTab] = useState<'pools' | 'volumes'>('pools')
  const [poolDialog, setPoolDialog] = useState<StoragePoolDetail | 'new' | null>(null)
  const [volumeDialog, setVolumeDialog] = useState<StorageVolume | 'new' | null>(null)
  const [pendingDelete, setPendingDelete] = useState<PendingDelete | null>(null)
  const [busy, setBusy] = useState(false)
  const mutating = useRef(false)
  const loadSequence = useRef(0)

  const load = useCallback((signal?: AbortSignal) => {
    const sequence = ++loadSequence.current
    return api.storage(signal)
      .then((next) => {
        if (sequence === loadSequence.current) {
          setData(next)
          setError(null)
        }
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
    const timer = window.setInterval(() => {
      if (!mutating.current) load()
    }, 5000)
    return () => { controller.abort(); window.clearInterval(timer) }
  }, [load])

  const run = useCallback(async (action: () => Promise<unknown>, success: string) => {
    mutating.current = true
    setBusy(true)
    try {
      await action()
      onNotify('success', success)
      setPoolDialog(null)
      setVolumeDialog(null)
      setPendingDelete(null)
    } catch (cause) {
      onNotify('error', 'Storage change failed', (cause as Error).message)
      setPendingDelete(null)
    } finally {
      await load()
      setBusy(false)
      mutating.current = false
    }
  }, [load, onNotify])

  if (error && !data) {
    return <div className="banner banner-error"><div className="banner-body"><p>{error}</p></div></div>
  }
  if (!data) {
    return <div className="card"><div className="loading-wrap"><span className="spinner" /> Loading storage…</div></div>
  }

  const manageablePools = data.pools.filter((pool) => pool.manageable)
  const availableDrivers = data.clustered
    ? []
    : data.local_drivers.filter((driver) => driver.available)

  return (
    <>
      {data.clustered && (
        <div className="banner banner-warn">
          <div className="banner-body"><p>Clustered storage is shown read-only.</p></div>
        </div>
      )}
      {error && (
        <div className="banner banner-error"><div className="banner-body"><p>{error}</p></div></div>
      )}

      <div className="storage-toolbar">
        <div className="tabs storage-tabs" role="tablist" aria-label="Storage objects">
          <button className="tab" aria-selected={tab === 'pools'}
            onClick={() => setTab('pools')}>Pools</button>
          <button className="tab" aria-selected={tab === 'volumes'}
            onClick={() => setTab('volumes')}>Volumes</button>
        </div>
        {tab === 'pools' ? (
          <button className="btn btn-primary" disabled={busy || !canWrite || availableDrivers.length === 0}
            onClick={() => setPoolDialog('new')}>
            <PlusIcon /> New pool
          </button>
        ) : (
          <button className="btn btn-primary" disabled={busy || !canWrite || manageablePools.length === 0}
            onClick={() => setVolumeDialog('new')}>
            <PlusIcon /> New volume
          </button>
        )}
      </div>

      {tab === 'pools' ? (
        <PoolsTable pools={data.pools} busy={busy || !canWrite}
          onEdit={setPoolDialog}
          onDelete={(pool) => setPendingDelete({ kind: 'pool', pool })} />
      ) : (
        <VolumesTable volumes={data.volumes} busy={busy || !canWrite}
          onEdit={setVolumeDialog}
          onDelete={(volume) => setPendingDelete({ kind: 'volume', volume })} />
      )}

      {poolDialog && (
        <PoolDialog
          pool={poolDialog === 'new' ? null : poolDialog}
          drivers={availableDrivers.map((driver) => driver.name)}
          busy={busy}
          onCancel={() => setPoolDialog(null)}
          onSubmit={(request) => poolDialog === 'new'
            ? run(() => api.createStoragePool(request), `Created pool ${request.name}`)
            : run(() => api.updateStoragePool(poolDialog.name, request),
              `Updated pool ${poolDialog.name}`)}
        />
      )}

      {volumeDialog && (
        <VolumeDialog
          volume={volumeDialog === 'new' ? null : volumeDialog}
          pools={manageablePools}
          busy={busy}
          onCancel={() => setVolumeDialog(null)}
          onSubmit={(pool, request) => volumeDialog === 'new'
            ? run(() => api.createStorageVolume(pool, request), `Created volume ${request.name}`)
            : run(() => api.updateStorageVolume(pool, volumeDialog.name, request),
              `Updated volume ${volumeDialog.name}`)}
        />
      )}

      {pendingDelete?.kind === 'pool' && (
        <PoolDeleteDialog
          pool={pendingDelete.pool}
          busy={busy}
          onCancel={() => setPendingDelete(null)}
          onConfirm={() => run(() => api.deleteStoragePool(
            pendingDelete.pool.name, true,
            pendingDelete.pool.delete_plan),
            pendingDelete.pool.driver === 'zfs'
              ? `Detached pool ${pendingDelete.pool.name}`
              : `Deleted pool ${pendingDelete.pool.name}`)}
        />
      )}
      {pendingDelete?.kind === 'volume' && (
        <ConfirmDialog
          title={`Delete volume ${pendingDelete.volume.name}?`}
          message={`This permanently removes ${pendingDelete.volume.pool}/${pendingDelete.volume.name}. Attached volumes cannot be deleted.`}
          confirmLabel="Delete volume" danger busy={busy}
          api={calls.deleteStorageVolume(pendingDelete.volume.pool, pendingDelete.volume.name)}
          onCancel={() => setPendingDelete(null)}
          onConfirm={() => run(
            () => api.deleteStorageVolume(pendingDelete.volume.pool, pendingDelete.volume.name),
            `Deleted volume ${pendingDelete.volume.name}`)}
        />
      )}
    </>
  )
}

function PoolsTable({ pools, busy, onEdit, onDelete }: {
  pools: StoragePoolDetail[]
  busy: boolean
  onEdit: (pool: StoragePoolDetail) => void
  onDelete: (pool: StoragePoolDetail) => void
}) {
  if (pools.length === 0) {
    return <div className="card"><div className="empty"><h3>No storage pools</h3><p>Create a local pool to store instances and volumes.</p></div></div>
  }
  return (
    <div className="card table-scroll">
      <table className="ctable storage-table">
        <thead><tr><th>Name</th><th>Driver</th><th>Capacity</th><th>Instances</th><th>Volumes</th><th>Status</th><th aria-label="Actions" /></tr></thead>
        <tbody>{pools.map((pool) => {
          const percent = pool.total ? Math.min(100, pool.used / pool.total * 100) : 0
          const attached = pool.delete_plan.attached_instances
          return (
            <tr key={pool.name}>
              <td><strong>{pool.name}</strong>{pool.root && <span className="badge badge-info storage-root">root</span>}<div className="res-sub mono">{pool.source || 'daemon managed'}</div></td>
              <td><span className="badge badge-dim">{pool.driver}</span></td>
              <td className="storage-capacity"><div>{pool.total ? `${bytes(pool.used)} of ${bytes(pool.total)}` : 'not reported'}</div>{pool.total > 0 && <div className="storage-meter"><span style={{ width: `${percent}%` }} /></div>}</td>
              <td className="storage-instances">
                <PoolInstances instances={pool.delete_plan.instances} />
                {attached.length > 0 && <div className="res-sub" title={`Attached through custom volumes: ${attached.join(', ')}`}>attached: {attached.join(', ')}</div>}
              </td>
              <td className="num">{pool.volume_count}</td>
              <td>{pool.manageable ? <span className="badge badge-ok">managed</span> : <span className="badge badge-dim" title={pool.read_only_reason}>read-only</span>}</td>
              <td className="storage-actions">{pool.manageable && <><button className="btn" disabled={busy} onClick={() => onEdit(pool)}>Edit</button><button className="btn btn-danger" disabled={busy} onClick={() => onDelete(pool)}>{poolDeleteCount(pool) > 0 ? 'Force delete' : 'Delete'}</button></>}</td>
            </tr>
          )
        })}</tbody>
      </table>
    </div>
  )
}

function PoolInstances({ instances }: { instances: string[] }) {
  const [expanded, setExpanded] = useState(false)
  if (instances.length === 0) return <span className="res-sub">none</span>

  const visible = expanded ? instances : instances.slice(0, 8)
  const hiddenCount = instances.length - 8
  return (
    <div className="storage-instance-list">
      {visible.map((instance) => (
        <span className="storage-instance" key={instance}>{instance}</span>
      ))}
      {hiddenCount > 0 && (
        <button type="button" className="storage-instance-more"
          aria-expanded={expanded} onClick={() => setExpanded((value) => !value)}>
          {expanded ? 'Show less' : `Show ${hiddenCount} more`}
        </button>
      )}
    </div>
  )
}

function PoolDeleteDialog({ pool, busy, onCancel, onConfirm }: {
  pool: StoragePoolDetail
  busy: boolean
  onCancel: () => void
  onConfirm: () => void
}) {
  const [confirmation, setConfirmation] = useState('')
  const plan = pool.delete_plan
  const count = poolDeleteCount(pool)
  const unsupported = plan.other_references.length > 0 || plan.other_volumes.length > 0
  const isZfs = pool.driver === 'zfs'
  const zpool = (pool.config['zfs.pool_name'] || pool.source || pool.name).split('/', 1)[0]
  const groups = [
    ['Instances and virtual machines stored here', plan.instances],
    ['Instances attached to custom volumes', plan.attached_instances],
    ['Cached images', plan.images],
    ['Custom volumes', plan.custom_volumes],
    ['Profiles to update', plan.profiles],
    ['Unsupported references', plan.other_references],
    ['Unsupported volume types', plan.other_volumes],
  ] as const

  return (
    <Modal title={`${isZfs ? 'Remove' : 'Delete'} pool ${pool.name}?`} onClose={busy ? () => {} : onCancel}
      api={calls.deleteStoragePool(pool.name, true, plan)}
      footer={<><button className="btn" onClick={onCancel} disabled={busy}>Cancel</button><button className="btn btn-danger" disabled={busy || unsupported || confirmation !== pool.name} onClick={onConfirm}>{busy && <span className="spinner" />}{isZfs ? 'Remove from LXD' : count > 0 ? 'Delete everything' : 'Delete pool'}</button></>}>
      <div className="pool-delete-dialog">
        <p>{count > 0
          ? `This permanently deletes ${count} resource${count === 1 ? '' : 's'} before ${isZfs ? 'removing the pool from LXD' : 'deleting the pool'}.`
          : isZfs
            ? 'This removes the empty storage pool from LXD.'
            : 'This permanently deletes the empty storage pool.'}</p>
        {count > 0 && <p className="hint">Instances attached to custom volumes are deleted even when their root disk is on another pool. Cached images are removed daemon-wide. This operation cannot be rolled back if a later step fails.</p>}
        {isZfs && (
          <div className="banner banner-warn"><div className="banner-body"><p>
            The backing zpool <strong className="mono">{zpool}</strong> will be preserved.
            After managed resources are removed, export it on the host when prompted and retry
            to remove only the LXD registration.
          </p></div></div>
        )}
        {groups.map(([label, resources]) => resources.length > 0 && (
          <div key={label} className="pool-delete-group">
            <strong>{label}</strong>
            <ul>{resources.map((resource) => <li key={resource} className="mono">{resource}</li>)}</ul>
          </div>
        ))}
        {unsupported && (
          <div className="banner banner-error"><div className="banner-body"><p>
            lemondx cannot safely remove: {[...plan.other_references,
              ...plan.other_volumes].join(', ')}
          </p></div></div>
        )}
        <label className="field">
          <span>Type <strong className="mono">{pool.name}</strong> to confirm</span>
          <input className="input mono" value={confirmation} disabled={busy || unsupported}
            autoComplete="off" onChange={(event) => setConfirmation(event.target.value)} />
        </label>
      </div>
    </Modal>
  )
}

function poolDeleteCount(pool: StoragePoolDetail) {
  const plan = pool.delete_plan
  return plan.instances.length + plan.attached_instances.length
    + plan.images.length + plan.custom_volumes.length
    + plan.profiles.length + plan.other_references.length + plan.other_volumes.length
}

function VolumesTable({ volumes, busy, onEdit, onDelete }: {
  volumes: StorageVolume[]
  busy: boolean
  onEdit: (volume: StorageVolume) => void
  onDelete: (volume: StorageVolume) => void
}) {
  if (volumes.length === 0) {
    return <div className="card"><div className="empty"><h3>No storage volumes</h3><p>Custom and instance volumes will appear here.</p></div></div>
  }
  return (
    <div className="card table-scroll">
      <table className="ctable storage-table">
        <thead><tr><th>Name</th><th>Pool</th><th>Type</th><th>Content</th><th>Size</th><th>Attached</th><th aria-label="Actions" /></tr></thead>
        <tbody>{volumes.map((volume) => (
          <tr key={`${volume.pool}/${volume.type}/${volume.name}`}>
            <td><strong>{volume.name}</strong>{volume.description && <div className="res-sub">{volume.description}</div>}</td>
            <td>{volume.pool}</td><td><span className="badge badge-dim">{volume.type}</span></td>
            <td>{volume.content_type}</td><td>{volume.size || 'unlimited'}</td>
            <td className="num">{volume.used_by.length || '—'}</td>
            <td className="storage-actions">{volume.manageable ? <><button className="btn" disabled={busy} onClick={() => onEdit(volume)}>Edit</button><button className="btn btn-danger" disabled={busy || volume.used_by.length > 0} onClick={() => onDelete(volume)}>Delete</button></> : <span className="faint">read-only</span>}</td>
          </tr>
        ))}</tbody>
      </table>
    </div>
  )
}

function PoolDialog({ pool, drivers, busy, onCancel, onSubmit }: {
  pool: StoragePoolDetail | null
  drivers: string[]
  busy: boolean
  onCancel: () => void
  onSubmit: (request: StoragePoolRequest) => void
}) {
  const [name, setName] = useState(pool?.name ?? '')
  const [driver, setDriver] = useState(pool?.driver ?? drivers[0] ?? 'dir')
  const [source, setSource] = useState(pool?.source ?? '')
  const [size, setSize] = useState(pool?.config.size ?? '')
  const [description, setDescription] = useState(pool?.description ?? '')
  const [config, setConfig] = useState('')
  const [configError, setConfigError] = useState('')
  const request = (): StoragePoolRequest => ({ name, driver, source: source || undefined,
    size: !pool && source ? undefined : size || undefined,
    description, config: parseConfig(config) })
  const submit = (event: FormEvent) => {
    event.preventDefault()
    try {
      onSubmit(request())
    } catch (cause) {
      setConfigError((cause as Error).message)
    }
  }
  return (
    <Modal title={pool ? `Edit ${pool.name}` : 'Create storage pool'} onClose={onCancel}
      api={attempt(() => pool
        ? calls.updateStoragePool(pool.name, request())
        : calls.createStoragePool(request()))}
      footer={<><button className="btn" onClick={onCancel} disabled={busy}>Cancel</button><button className="btn btn-primary" form="storage-pool-form" disabled={busy}>{busy && <span className="spinner" />}{pool ? 'Save changes' : 'Create pool'}</button></>}>
      <form id="storage-pool-form" className="storage-form" onSubmit={submit}>
        <div className="grid-2"><Field label="Name"><input className="input" required value={name} disabled={!!pool} onChange={(event) => setName(event.target.value)} /></Field><Field label="Driver"><select className="select" value={driver} disabled={!!pool} onChange={(event) => setDriver(event.target.value)}>{drivers.map((item) => <option key={item}>{item}</option>)}</select></Field></div>
        <Field label="Source"><input className="input mono" value={source} disabled={!!pool} placeholder={driver === 'dir' ? '/srv/lemondx' : 'optional device or existing pool'} onChange={(event) => setSource(event.target.value)} /></Field>
        {driver !== 'dir' && (!source || !!pool?.config.size) && <Field label="Size"><input className="input" value={size} placeholder="30GiB" onChange={(event) => setSize(event.target.value)} /></Field>}
        <Field label="Description"><input className="input" value={description} onChange={(event) => setDescription(event.target.value)} /></Field>
        <Field label="Advanced local options"><textarea className="textarea mono" rows={4} value={config} placeholder="volume.size=20GiB" onChange={(event) => { setConfig(event.target.value); setConfigError('') }} />{configError && <span className="field-error">{configError}</span>}</Field>
        {!pool && driver !== 'dir' && !source && <p className="hint">Without a source, the daemon creates convenient loop-backed storage. Prefer a dedicated disk or partition for production.</p>}
      </form>
    </Modal>
  )
}

function VolumeDialog({ volume, pools, busy, onCancel, onSubmit }: {
  volume: StorageVolume | null
  pools: StoragePoolDetail[]
  busy: boolean
  onCancel: () => void
  onSubmit: (pool: string, request: StorageVolumeRequest) => void
}) {
  const [pool, setPool] = useState(volume?.pool ?? pools[0]?.name ?? '')
  const [name, setName] = useState(volume?.name ?? '')
  const [contentType, setContentType] = useState<'filesystem' | 'block'>((volume?.content_type as 'filesystem' | 'block') ?? 'filesystem')
  const [size, setSize] = useState(volume?.size ?? '')
  const [description, setDescription] = useState(volume?.description ?? '')
  const [config, setConfig] = useState('')
  const [configError, setConfigError] = useState('')
  const request = (): StorageVolumeRequest => ({ name, content_type: contentType,
    size: size || undefined, description, config: parseConfig(config) })
  const submit = (event: FormEvent) => {
    event.preventDefault()
    try {
      onSubmit(pool, request())
    } catch (cause) {
      setConfigError((cause as Error).message)
    }
  }
  return (
    <Modal title={volume ? `Edit ${volume.name}` : 'Create custom volume'} onClose={onCancel}
      api={attempt(() => volume
        ? calls.updateStorageVolume(pool, volume.name, request())
        : calls.createStorageVolume(pool, request()))}
      footer={<><button className="btn" onClick={onCancel} disabled={busy}>Cancel</button><button className="btn btn-primary" form="storage-volume-form" disabled={busy}>{busy && <span className="spinner" />}{volume ? 'Save changes' : 'Create volume'}</button></>}>
      <form id="storage-volume-form" className="storage-form" onSubmit={submit}>
        <div className="grid-2"><Field label="Pool"><select className="select" value={pool} disabled={!!volume} onChange={(event) => setPool(event.target.value)}>{pools.map((item) => <option key={item.name}>{item.name}</option>)}</select></Field><Field label="Name"><input className="input" required value={name} disabled={!!volume} onChange={(event) => setName(event.target.value)} /></Field></div>
        <div className="grid-2"><Field label="Content type"><select className="select" value={contentType} disabled={!!volume} onChange={(event) => setContentType(event.target.value as 'filesystem' | 'block')}><option value="filesystem">Filesystem</option><option value="block">Block</option></select></Field><Field label="Size"><input className="input" value={size} placeholder="20GiB" onChange={(event) => setSize(event.target.value)} /></Field></div>
        <Field label="Description"><input className="input" value={description} onChange={(event) => setDescription(event.target.value)} /></Field>
        <Field label="Advanced volume options"><textarea className="textarea mono" rows={4} value={config} placeholder="snapshots.schedule=@daily" onChange={(event) => { setConfig(event.target.value); setConfigError('') }} />{configError && <span className="field-error">{configError}</span>}</Field>
        {contentType === 'block' && volume && <p className="hint">Block volumes can grow but cannot be shrunk.</p>}
      </form>
    </Modal>
  )
}

/** The request, or null while the advanced options do not parse. */
function attempt(build: () => ApiCall): ApiCall | null {
  try {
    return build()
  } catch {
    return null
  }
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return <label className="field"><span>{label}</span>{children}</label>
}

function parseConfig(text: string): Record<string, string> | undefined {
  const config: Record<string, string> = {}
  for (const [index, line] of text.split('\n').entries()) {
    const trimmed = line.trim()
    if (!trimmed) continue
    const separator = trimmed.indexOf('=')
    if (separator < 1) throw new Error(`Advanced option on line ${index + 1} must be KEY=VALUE.`)
    config[trimmed.slice(0, separator).trim()] = trimmed.slice(separator + 1).trim()
  }
  return Object.keys(config).length ? config : undefined
}