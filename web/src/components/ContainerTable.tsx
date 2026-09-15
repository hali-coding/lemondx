import type { Container, CreateProgress, HealthRecord, StateAction } from '../lib/types'
import { useCanWrite } from '../hooks/useAuth'
import { bytes } from '../lib/format'
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

export function ContainerTable({
  containers, creates, health, selected, busy, onSelect, onAction, onDelete, onCreate, canCreate,
}: Props) {
  const canWrite = useCanWrite()
  const inProgress = new Map(
    creates.filter((c) => c.finished_at === null).map((c) => [c.name, c]))
  // The daemon lists an instance only once its image is unpacked, so a create
  // still downloading gets a row of its own until then.
  const known = new Set(containers.map((c) => c.name))
  const placeholders = [...inProgress.values()].filter((c) => !known.has(c.name))

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
      <table className="ctable">
        <thead>
          <tr>
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
    </div>
  )
}
