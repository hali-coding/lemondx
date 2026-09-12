import type { Container, StateAction } from '../lib/types'
import { bytes } from '../lib/format'
import { StatusBadge } from './StatusBadge'
import { PlayIcon, PlusIcon, RestartIcon, StopIcon, TrashIcon } from './Icons'

interface Props {
  containers: Container[]
  selected: string | null
  busy: Record<string, boolean>
  onSelect: (name: string) => void
  onAction: (name: string, action: StateAction) => void
  onDelete: (name: string) => void
  onCreate: () => void
  canCreate: boolean
}

export function ContainerTable({
  containers, selected, busy, onSelect, onAction, onDelete, onCreate, canCreate,
}: Props) {
  if (containers.length === 0) {
    return (
      <div className="card">
        <div className="empty">
          <h3>No containers yet</h3>
          <p>
            {canCreate
              ? 'Create your first one — images are pulled automatically.'
              : 'Set up LXD above, then create your first container.'}
          </p>
          <button className="btn btn-primary" onClick={onCreate} disabled={!canCreate}>
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
          {containers.map((container) => {
            const isBusy = busy[container.name]
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
                  {isBusy
                    ? <span className="badge badge-warn"><span className="spinner" />working</span>
                    : <StatusBadge status={container.status} />}
                </td>
                <td className="optional dim truncate" style={{ maxWidth: 190 }}>
                  {container.image_alias || container.image || '—'}
                </td>
                <td className="optional mono num">{container.ipv4[0] ?? '—'}</td>
                <td className="optional num dim">{bytes(container.memory_usage)}</td>
                <td onClick={(event) => event.stopPropagation()}>
                  <div className="row-actions">
                    {running || frozen ? (
                      <>
                        <button
                          className="btn btn-sm btn-icon"
                          title="Restart"
                          aria-label={`Restart ${container.name}`}
                          disabled={isBusy}
                          onClick={() => onAction(container.name, 'restart')}
                        >
                          <RestartIcon />
                        </button>
                        <button
                          className="btn btn-sm btn-icon"
                          title="Stop"
                          aria-label={`Stop ${container.name}`}
                          disabled={isBusy}
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
                        disabled={isBusy}
                        onClick={() => onAction(container.name, 'start')}
                      >
                        <PlayIcon />
                      </button>
                    )}
                    <button
                      className="btn btn-sm btn-icon btn-danger"
                      title="Delete"
                      aria-label={`Delete ${container.name}`}
                      disabled={isBusy}
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
