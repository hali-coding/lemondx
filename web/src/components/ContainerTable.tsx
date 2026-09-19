import { useState } from 'react'
import type {
  CreateProgress, HealthRecord, InstanceRef, ScopedContainer, StateAction,
} from '../lib/types'
import { useCanWrite } from '../hooks/useAuth'
import { bytes } from '../lib/format'
import { keyOf } from '../lib/instance'
import { ConfirmDialog } from './ConfirmDialog'
import { CopyButton } from './CopyButton'
import { AppDot, HealthDot, PendingHealth } from './HealthDot'
import { StatusBadge } from './StatusBadge'
import { ExternalIcon, PlayIcon, PlusIcon, RestartIcon, StopIcon, TrashIcon } from './Icons'

interface Props {
  containers: ScopedContainer[]
  /** Creates the server is running or recently finished, including other tabs'. */
  creates: CreateProgress[]
  /** Latest health check per row, keyed by keyOf(); each node judges its own. */
  health: Record<string, HealthRecord>
  /**
   * Nodes whose monitor runs ('' for rows without a node, in a view of this
   * host alone): a running row there with no record yet is pending.
   */
  monitored: Set<string>
  /** Open the full output of a row's app check. */
  onShowAppCheck: (container: ScopedContainer) => void
  /** The selected row's key, from `keyOf`. */
  selected: string | null
  /** Busy rows, keyed by `keyOf`. */
  busy: Record<string, boolean>
  onSelect: (container: ScopedContainer) => void
  onAction: (container: ScopedContainer, action: StateAction) => void
  /** Runs one action over several; resolves false if the request never landed. */
  onBulkAction: (instances: InstanceRef[], action: StateAction) => Promise<boolean>
  onDelete: (container: ScopedContainer) => void
  onCreate: () => void
  canCreate: boolean
  /** Which node this lemondx is; rows on any other are read-only here. */
  localNode: string
  /** Node name -> its own UI address, for opening a remote instance where it lives. */
  nodeUrls: Record<string, string>
  /** Whether the view spans more than this node, so rows say where they are. */
  showNodes: boolean
}

function refOf(container: ScopedContainer, localNode: string): InstanceRef {
  return { node: container.node ?? localNode, name: container.name }
}

const STAGE_LABEL: Record<CreateProgress['stage'], string> = {
  creating: 'creating',
  starting: 'starting',
  bootstrapping: 'bootstrapping',
  done: 'done',
}

function stageText(progress: CreateProgress) {
  return progress.stage === 'bootstrapping' && progress.modules
    ? `bootstrapping · ${progress.modules} module${progress.modules === 1 ? '' : 's'}`
    : STAGE_LABEL[progress.stage]
}

const BULK_LABEL: Record<'start' | 'stop', string> = { start: 'Start', stop: 'Stop' }

/** How an instance is named in a confirmation: with its node, once that matters. */
function describe(instance: InstanceRef) {
  return `${instance.name} on ${instance.node}`
}

/** Name the containers an action is about, without an unbounded wall of text. */
function nameList(names: string[], limit = 8) {
  return names.length <= limit
    ? names.join(', ')
    : `${names.slice(0, limit).join(', ')} and ${names.length - limit} more`
}

export function ContainerTable({
  containers, creates, health, selected, busy, onSelect, onAction, onBulkAction, onDelete,
  onShowAppCheck, monitored,
  onCreate, canCreate, localNode, nodeUrls, showNodes,
}: Props) {
  const canWrite = useCanWrite()
  // Ticked names, not containers: the list is replaced by every poll, and a
  // name that has since gone is filtered out below rather than tracked, so
  // nothing has to prune this when a container disappears.
  const [ticked, setTicked] = useState<ReadonlySet<string>>(new Set())
  // The action *and* the names it was opened for. Deriving the names while the
  // dialog is up would empty them the moment the request marks those rows busy,
  // and what the user confirmed is exactly what should be acted on.
  const [confirming, setConfirming] =
    useState<{ action: 'start' | 'stop'; instances: InstanceRef[] } | null>(null)
  const [applying, setApplying] = useState(false)
  const inProgress = new Map(
    creates.filter((c) => c.finished_at === null).map((c) => [c.name, c]))
  // The daemon lists an instance only once its image is unpacked, so a create
  // still downloading gets a row of its own until then.
  // Creates are this node's own, so a placeholder only belongs on a view that
  // includes it; another node's creates show once its daemon lists them.
  const local = (c: ScopedContainer) => (c.node ?? localNode) === localNode
  const known = new Set(containers.filter(local).map((c) => c.name))
  const placeholders = [...inProgress.values()].filter((c) => !known.has(c.name))

  // Acting on a container mid-create would pull the rug out from under its
  // bootstrap, so those cannot be ticked -- same rule as the row's own buttons.
  const selectable = containers.filter(
    (c) => !busy[keyOf(c)] && !(local(c) && inProgress.has(c.name)))
  const chosen = selectable.filter((c) => ticked.has(keyOf(c)))
  const allTicked = selectable.length > 0 && chosen.length === selectable.length

  const tick = (name: string, on: boolean) => {
    setTicked((current) => {
      const next = new Set(current)
      if (on) next.add(name)
      else next.delete(name)
      return next
    })
  }

  const applyBulk = async () => {
    if (!confirming) return
    setApplying(true)
    const landed = await onBulkAction(confirming.instances, confirming.action)
    setApplying(false)
    setConfirming(null)
    // Per-container failures are reported by the caller; the request either
    // way is over, so the tick marks have served their purpose. One that never
    // landed keeps them, to try again.
    if (landed) setTicked(new Set())
  }

  if (containers.length === 0 && placeholders.length === 0) {
    return (
      <div className="card">
        <div className="empty">
          <h3>No containers yet</h3>
          <p>
            {canCreate
              ? 'Create your first one — images are pulled automatically.'
              : 'Set up LXD above, then create your first container.'}
          </p>
          <button className="btn btn-primary" onClick={onCreate} disabled={!canCreate || !canWrite}>
            <PlusIcon /> New container
          </button>
        </div>
      </div>
    )
  }

  return (
    <div className="card">
      {chosen.length > 0 && (
        <div className="bulk-bar">
          <span className="bulk-count">
            {chosen.length} selected
          </span>
          <span className="faint truncate bulk-names">
            {nameList(chosen.map((c) => c.name), 4)}
          </span>
          <div className="bulk-actions">
            <button className="btn btn-sm" disabled={!canWrite || applying}
              onClick={() => setConfirming({
                action: 'start', instances: chosen.map((c) => refOf(c, localNode)) })}>
              <PlayIcon /> Start
            </button>
            <button className="btn btn-sm" disabled={!canWrite || applying}
              onClick={() => setConfirming({
                action: 'stop', instances: chosen.map((c) => refOf(c, localNode)) })}>
              <StopIcon /> Stop
            </button>
            <button className="btn btn-sm btn-ghost" disabled={applying}
              onClick={() => setTicked(new Set())}>
              Clear
            </button>
          </div>
        </div>
      )}
      <table className="ctable">
        <thead>
          <tr>
            <th className="tick">
              <input
                type="checkbox"
                checked={allTicked}
                // Some but not all: neither state describes it honestly.
                ref={(box) => {
                  if (box) box.indeterminate = chosen.length > 0 && !allTicked
                }}
                disabled={selectable.length === 0 || !canWrite}
                aria-label={allTicked ? 'Clear selection' : 'Select all containers'}
                onChange={(event) => setTicked(
                  event.target.checked ? new Set(selectable.map(keyOf)) : new Set())}
              />
            </th>
            <th>Name</th>
            {showNodes && <th>Node</th>}
            <th>State</th>
            <th className="optional">Image</th>
            <th className="optional">IPv4</th>
            <th className="optional">Memory</th>
            <th><span className="sr-only">Actions</span></th>
          </tr>
        </thead>
        <tbody>
          {placeholders.map((progress) => (
            <tr key={`creating-${progress.name}`} className="row-pending">
              <td className="tick" />
              <td>
                <div className="cname">
                  {progress.name}
                  {progress.type === 'virtual-machine' && <span className="vm-tag">VM</span>}
                </div>
                {progress.template && (
                  <div className="cdesc truncate">from {progress.template}</div>
                )}
              </td>
              {showNodes && <td className="dim">{localNode}</td>}
              <td>
                <span className="badge badge-warn">
                  <span className="spinner" />{stageText(progress)}
                </span>
              </td>
              <td className="optional dim truncate" style={{ maxWidth: 190 }}>
                {progress.image}
              </td>
              <td className="optional mono num">—</td>
              <td className="optional num dim">—</td>
              <td />
            </tr>
          ))}
          {containers.map((container) => {
            const here = local(container)
            const rowKey = keyOf(container)
            const progress = here ? inProgress.get(container.name) : undefined
            // Starting or stopping it mid-create would pull the rug out from
            // under the bootstrap; deleting stays possible, to abandon one.
            const isBusy = busy[rowKey] || !!progress
            const running = container.status === 'Running'
            const frozen = container.status === 'Frozen'
            return (
              <tr
                key={rowKey}
                aria-selected={selected === rowKey}
                onClick={() => onSelect(container)}
                tabIndex={0}
                onKeyDown={(event) => {
                  if (event.key === 'Enter' || event.key === ' ') {
                    event.preventDefault()
                    onSelect(container)
                  }
                }}
              >
                <td className="tick" onClick={(event) => event.stopPropagation()}>
                  <input
                    type="checkbox"
                    checked={ticked.has(rowKey)}
                    disabled={isBusy || !canWrite}
                    aria-label={`Select ${container.name}`}
                    onKeyDown={(event) => event.stopPropagation()}
                    onChange={(event) => tick(rowKey, event.target.checked)}
                  />
                </td>
                <td>
                  <div className="cname">
                    {container.name}
                    {container.type === 'virtual-machine' && <span className="vm-tag">VM</span>}
                    {container.ephemeral && <span className="vm-tag">EPH</span>}
                  </div>
                  {container.description && (
                    <div className="cdesc truncate">{container.description}</div>
                  )}
                  {container.template && (
                    <div className="cdesc truncate">from {container.template}</div>
                  )}
                </td>
                {showNodes && (
                  <td className="dim">
                    {container.node ?? localNode}
                    {here && <span className="badge badge-dim">here</span>}
                  </td>
                )}
                <td>
                  {progress
                    ? <span className="badge badge-warn"><span className="spinner" />{stageText(progress)}</span>
                    : busy[rowKey]
                      ? <span className="badge badge-warn"><span className="spinner" />working</span>
                      : <>
                        <StatusBadge status={container.status} />
                        {/* Each node's own monitor judges its instances;
                            these are whatever the owning node last said. */}
                        {container.status === 'Running' && (health[rowKey] ? (
                          <>
                            <HealthDot record={health[rowKey]} />
                            {health[rowKey].app && (
                              <AppDot app={health[rowKey].app!}
                                onOpen={() => onShowAppCheck(container)} />
                            )}
                          </>
                        ) : monitored.has(container.node ?? '') && <PendingHealth />)}
                      </>}
                </td>
                <td className="optional dim truncate" style={{ maxWidth: 190 }}>
                  {container.image_alias || container.image || '—'}
                </td>
                <td className="optional mono num">
                  {container.ipv4[0] ? (
                    <span className="ip-cell">
                      {container.ipv4[0]}
                      <CopyButton text={container.ipv4[0]}
                        label={`IP address of ${container.name}`} />
                    </span>
                  ) : '—'}
                </td>
                <td className="optional num dim">{bytes(container.memory_usage)}</td>
                <td onClick={(event) => event.stopPropagation()}>
                  <div className="row-actions">
                    {running || frozen ? (
                      <>
                        <button
                          className="btn btn-sm btn-icon"
                          title="Restart"
                          aria-label={`Restart ${container.name}`}
                          disabled={isBusy || !canWrite}
                          onClick={() => onAction(container, 'restart')}
                        >
                          <RestartIcon />
                        </button>
                        <button
                          className="btn btn-sm btn-icon"
                          title="Stop"
                          aria-label={`Stop ${container.name}`}
                          disabled={isBusy || !canWrite}
                          onClick={() => onAction(container, 'stop')}
                        >
                          <StopIcon />
                        </button>
                      </>
                    ) : (
                      <button
                        className="btn btn-sm btn-icon"
                        title="Start"
                        aria-label={`Start ${container.name}`}
                        disabled={isBusy || !canWrite}
                        onClick={() => onAction(container, 'start')}
                      >
                        <PlayIcon />
                      </button>
                    )}
                    <button
                      className="btn btn-sm btn-icon btn-danger"
                      title="Delete"
                      aria-label={`Delete ${container.name}`}
                      disabled={busy[rowKey] || !canWrite}
                      onClick={() => onDelete(container)}
                    >
                      <TrashIcon />
                    </button>
                    {/* Managing it here is the normal path; this is for when
                        you want that node's own UI -- its storage, its logs. */}
                    {!here && nodeUrls[container.node ?? ''] && (
                      <a
                        className="btn btn-sm btn-icon"
                        href={`${nodeUrls[container.node ?? '']}/`}
                        target="_blank"
                        rel="noreferrer"
                        title={`Open ${container.node}'s own UI`}
                        aria-label={`Open ${container.node}'s own UI`}
                        onClick={(event) => event.stopPropagation()}
                      >
                        <ExternalIcon />
                      </a>
                    )}
                  </div>
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>

      {confirming && (
        <ConfirmDialog
          title={`${BULK_LABEL[confirming.action]} ${confirming.instances.length} `
            + `container${confirming.instances.length === 1 ? '' : 's'}?`}
          message={confirming.action === 'stop'
            ? `${nameList(confirming.instances.map(describe))} will be shut down. Anything `
              + 'running inside them stops, and unsaved work in them is lost.'
            : `${nameList(confirming.instances.map(describe))} will be started. One that is `
              + 'already running is reported as such and left alone.'}
          confirmLabel={BULK_LABEL[confirming.action]}
          danger={confirming.action === 'stop'}
          busy={applying}
          onConfirm={applyBulk}
          onCancel={() => setConfirming(null)}
        />
      )}
    </div>
  )
}
