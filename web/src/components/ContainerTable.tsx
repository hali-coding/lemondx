import { useState } from 'react'
import type { Container, CreateProgress, HealthRecord, StateAction } from '../lib/types'
import { useCanWrite } from '../hooks/useAuth'
import { bytes } from '../lib/format'
import { ConfirmDialog } from './ConfirmDialog'
import { CopyButton } from './CopyButton'
import { HealthDot } from './HealthDot'
import { StatusBadge } from './StatusBadge'
import { PlayIcon, PlusIcon, RestartIcon, StopIcon, TrashIcon } from './Icons'

interface Props {
  containers: Container[]
  /** Creates the server is running or recently finished, including other tabs'. */
  creates: CreateProgress[]
  /** Latest health check per instance name; absent when never checked. */
  health: Record<string, HealthRecord>
  selected: string | null
  busy: Record<string, boolean>
  onSelect: (name: string) => void
  onAction: (name: string, action: StateAction) => void
  /** Runs one action over several; resolves false if the request never landed. */
  onBulkAction: (names: string[], action: StateAction) => Promise<boolean>
  onDelete: (name: string) => void
  onCreate: () => void
  canCreate: boolean
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

/** Name the containers an action is about, without an unbounded wall of text. */
function nameList(names: string[], limit = 8) {
  return names.length <= limit
    ? names.join(', ')
    : `${names.slice(0, limit).join(', ')} and ${names.length - limit} more`
}

export function ContainerTable({
  containers, creates, health, selected, busy, onSelect, onAction, onBulkAction, onDelete,
  onCreate, canCreate,
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
    useState<{ action: 'start' | 'stop'; names: string[] } | null>(null)
  const [applying, setApplying] = useState(false)
  const inProgress = new Map(
    creates.filter((c) => c.finished_at === null).map((c) => [c.name, c]))
  // The daemon lists an instance only once its image is unpacked, so a create
  // still downloading gets a row of its own until then.
  const known = new Set(containers.map((c) => c.name))
  const placeholders = [...inProgress.values()].filter((c) => !known.has(c.name))

  // Acting on a container mid-create would pull the rug out from under its
  // bootstrap, so those cannot be ticked -- same rule as the row's own buttons.
  const selectable = containers.filter((c) => !busy[c.name] && !inProgress.has(c.name))
  const chosen = selectable.filter((c) => ticked.has(c.name)).map((c) => c.name)
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
    const landed = await onBulkAction(confirming.names, confirming.action)
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
          <span className="faint truncate bulk-names">{nameList(chosen, 4)}</span>
          <div className="bulk-actions">
            <button className="btn btn-sm" disabled={!canWrite || applying}
              onClick={() => setConfirming({ action: 'start', names: chosen })}>
              <PlayIcon /> Start
            </button>
            <button className="btn btn-sm" disabled={!canWrite || applying}
              onClick={() => setConfirming({ action: 'stop', names: chosen })}>
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
                  event.target.checked ? new Set(selectable.map((c) => c.name)) : new Set())}
              />
            </th>
            <th>Name</th>
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
            const progress = inProgress.get(container.name)
            // Starting or stopping it mid-create would pull the rug out from
            // under the bootstrap; deleting stays possible, to abandon one.
            const isBusy = busy[container.name] || !!progress
            const running = container.status === 'Running'
            const frozen = container.status === 'Frozen'
            return (
              <tr
                key={container.name}
                aria-selected={selected === container.name}
                onClick={() => onSelect(container.name)}
                tabIndex={0}
                onKeyDown={(event) => {
                  if (event.key === 'Enter' || event.key === ' ') {
                    event.preventDefault()
                    onSelect(container.name)
                  }
                }}
              >
                <td className="tick" onClick={(event) => event.stopPropagation()}>
                  <input
                    type="checkbox"
                    checked={ticked.has(container.name)}
disabled={isBusy || !canWrite}
                    aria-label={`Select ${container.name}`}
                    onKeyDown={(event) => event.stopPropagation()}
                    onChange={(event) => tick(container.name, event.target.checked)}
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
                </td>
                <td>
                  {progress
                    ? <span className="badge badge-warn"><span className="spinner" />{stageText(progress)}</span>
                    : busy[container.name]
                      ? <span className="badge badge-warn"><span className="spinner" />working</span>
                      : <>
                        <StatusBadge status={container.status} />
                        {container.status === 'Running' && health[container.name] && (
                          <HealthDot record={health[container.name]} />
                        )}
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
                          onClick={() => onAction(container.name, 'restart')}
                        >
                          <RestartIcon />
                        </button>
                        <button
                          className="btn btn-sm btn-icon"
                          title="Stop"
                          aria-label={`Stop ${container.name}`}
                          disabled={isBusy || !canWrite}
                          onClick={() => onAction(container.name, 'stop')}
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
                        onClick={() => onAction(container.name, 'start')}
                      >
                        <PlayIcon />
                      </button>
                    )}
                    <button
                      className="btn btn-sm btn-icon btn-danger"
                      title="Delete"
                      aria-label={`Delete ${container.name}`}
                      disabled={busy[container.name] || !canWrite}
                      onClick={() => onDelete(container.name)}
                    >
                      <TrashIcon />
                    </button>
                  </div>
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>

      {confirming && (
        <ConfirmDialog
          title={`${BULK_LABEL[confirming.action]} ${confirming.names.length} `
            + `container${confirming.names.length === 1 ? '' : 's'}?`}
          message={confirming.action === 'stop'
            ? `${nameList(confirming.names)} will be shut down. Anything running inside `
              + 'them stops, and unsaved work in them is lost.'
            : `${nameList(confirming.names)} will be started. One that is already running `
              + 'is reported as such and left alone.'}
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
