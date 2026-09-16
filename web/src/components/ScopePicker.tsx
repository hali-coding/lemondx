import type { ClusterNode, NodeGroup, Scope } from '../lib/types'

interface Props {
  scope: Scope
  nodes: ClusterNode[]
  groups: NodeGroup[]
  onChange: (scope: Scope) => void
}

/**
 * How wide to look: this node, the whole cluster, one node or one group.
 *
 * A `<select>` rather than a two-way toggle because a cluster of any size is
 * mostly browsed one node at a time, and "local" is just the shortest of those
 * answers. Rendered only when there is more than one node to choose between.
 */
export function ScopePicker({ scope, nodes, groups, onChange }: Props) {
  const value = scope.kind === 'node' || scope.kind === 'group'
    ? `${scope.kind}:${scope.name}`
    : scope.kind

  return (
    <label className="scope-picker">
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
        <option value="local">This node</option>
        <option value="cluster">Whole cluster ({nodes.length} nodes)</option>
        {groups.length > 0 && (
          <optgroup label="Groups">
            {groups.map((group) => (
              <option key={group.name} value={`group:${group.name}`}>
                {group.name} ({group.members.length})
              </option>
            ))}
          </optgroup>
        )}
        <optgroup label="Nodes">
          {nodes.map((node) => (
            <option key={node.name} value={`node:${node.name}`}>
              {node.name}{node.self ? ' (this node)' : ''}
            </option>
          ))}
        </optgroup>
      </select>
    </label>
  )
}
