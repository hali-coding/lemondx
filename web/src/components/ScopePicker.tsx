import { scopeNodeCount } from '../hooks/useScope'
import type { ClusterNode, NodeGroup, Scope } from '../lib/types'

interface Props {
  scope: Scope
  nodes: ClusterNode[]
  groups: NodeGroup[]
  onChange: (scope: Scope) => void
}

/**
 * How wide to look: the whole cluster (the default), one group or one node.
 *
 * This node is listed once, by name, among the nodes -- a separate "This node"
 * entry beside the same node's name read as two different things. Anything
 * narrower than the whole cluster is marked, and says how much it leaves out,
 * so a filtered table is never taken for everything there is. Rendered only
 * when there is more than one node to choose between.
 */
export function ScopePicker({ scope, nodes, groups, onChange }: Props) {
  const value = scope.kind === 'node' || scope.kind === 'group'
    ? `${scope.kind}:${scope.name}`
    : scope.kind
  const narrowed = scope.kind !== 'cluster'
  const shown = scopeNodeCount(scope, nodes, groups)

  return (
    <div className={`scope-picker${narrowed ? ' scope-picker-narrowed' : ''}`}>
      <span className="faint">Showing</span>
      <select
        className="select"
        value={value}
        aria-label="Which nodes to show"
        onChange={(event) => {
          const picked = event.target.value
          if (picked === 'local' || picked === 'cluster') {
            onChange({ kind: picked })
            return
          }
          const [kind, ...rest] = picked.split(':')
          onChange({ kind: kind as 'node' | 'group', name: rest.join(':') })
        }}
      >
        <option value="cluster">Whole cluster ({nodes.length} nodes)</option>
        {groups.length > 0 && (
          <optgroup label="Groups">
            {groups.map((group) => (
              <option key={group.name} value={`group:${group.name}`}>
                {group.name} ({group.members.length} {group.members.length === 1 ? 'node' : 'nodes'})
              </option>
            ))}
          </optgroup>
        )}
        <optgroup label="Nodes">
          {nodes.map((node) => (
            // This node keeps the `local` scope: it is served here directly,
            // without going through the cluster's fan-out.
            <option key={node.name} value={node.self ? 'local' : `node:${node.name}`}>
              {node.name}{node.self ? ' (this node)' : ''}
            </option>
          ))}
        </optgroup>
      </select>
      {narrowed && (
        <>
          <span className="scope-picker-note">{shown} of {nodes.length} nodes</span>
          <button type="button" className="btn btn-sm" onClick={() => onChange({ kind: 'cluster' })}>
            Show all
          </button>
        </>
      )}
    </div>
  )
}
