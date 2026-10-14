import { useCallback, useEffect, useState } from 'react'
import { api } from '../lib/api'
import type { ClusterNode, NodeGroup, Scope } from '../lib/types'

const KEY = 'waterpump-scope'

// One object each, not a literal per call: the effective scope is a dependency
// of App's poll, so a fresh `{ kind: 'local' }` on every render would tear the
// poll down and restart it on every render -- which is every response.
const LOCAL: Scope = { kind: 'local' }
const CLUSTER: Scope = { kind: 'cluster' }

/**
 * How wide the Containers and Templates tabs are looking, shared by both so a
 * template's instances and the container list never disagree about which hosts
 * are in view.
 *
 * A cluster is one deployment, so a page opens on all of it: a narrower view
 * is something someone chose a moment ago, never what they find on arrival.
 * The choice is kept for the browser tab only (sessionStorage) -- a reload
 * keeps it, a new visit starts at the whole cluster again. A kept scope naming
 * a node that has since gone falls back to the whole cluster rather than
 * showing an error the user did not cause.
 */
export function useScope() {
  const [scope, setScope] = useState<Scope>(CLUSTER)
  const [nodes, setNodes] = useState<ClusterNode[]>([])
  const [groups, setGroups] = useState<NodeGroup[]>([])
  const [loaded, setLoaded] = useState(false)
  // Bumped to ask again. The first attempt runs before the user has logged in,
  // where it is refused and leaves us believing this waterpump is alone.
  const [generation, setGeneration] = useState(0)

  // The choices only exist once we know what the cluster holds, so the stored
  // scope is validated against that rather than trusted on its own.
  useEffect(() => {
    const controller = new AbortController()
    Promise.all([api.nodes(controller.signal, false), api.nodeGroups(controller.signal)])
      .then(([loadedNodes, loadedGroups]) => {
        setNodes(loadedNodes)
        setGroups(loadedGroups)
        setScope(restore(loadedNodes, loadedGroups))
        setLoaded(true)
      })
      .catch(() => setLoaded(true))
    return () => controller.abort()
  }, [generation])

  const choose = useCallback((next: Scope) => {
    setScope(next)
    try {
      window.sessionStorage.setItem(KEY, JSON.stringify(next))
    } catch { /* private mode: this session only */ }
  }, [])

  const reload = useCallback(() => setGeneration((current) => current + 1), [])

  // Federation is opt-in, so a waterpump joined to nothing should not grow a
  // control that only ever has one setting.
  const federated = nodes.length > 1
  return { scope: federated ? scope : LOCAL, choose, nodes, groups,
           federated, loaded, reload }
}

function restore(nodes: ClusterNode[], groups: NodeGroup[]): Scope {
  let stored: unknown
  try {
    // What earlier versions kept for good: dropped, so it cannot pin a view.
    window.localStorage.removeItem(KEY)
    stored = JSON.parse(window.sessionStorage.getItem(KEY) ?? 'null')
  } catch {
    return CLUSTER
  }
  const scope = stored as Scope | null
  if (!scope || typeof scope !== 'object') return CLUSTER
  if (scope.kind === 'local') return LOCAL
  if (scope.kind === 'cluster') return CLUSTER
  if (scope.kind === 'node') {
    const node = nodes.find((n) => n.name === scope.name)
    // The picker offers this node as `local`, so it has to come back as that.
    if (node) return node.self ? LOCAL : scope
  }
  if (scope.kind === 'group' && groups.some((g) => g.name === scope.name)) return scope
  return CLUSTER
}

/**
 * What to ask the API for. `all` rather than a list of node names for the whole
 * cluster, so a node that joined since this page loaded is still included.
 */
export function scopeTargets(
  scope: Scope,
): { nodes?: string[]; groups?: string[]; all?: boolean } {
  if (scope.kind === 'node') return { nodes: [scope.name] }
  if (scope.kind === 'group') return { groups: [scope.name] }
  if (scope.kind === 'cluster') return { all: true }
  return {}
}

export function scopeLabel(scope: Scope) {
  if (scope.kind === 'local') return 'This node'
  if (scope.kind === 'cluster') return 'Whole cluster'
  return scope.name
}

/** How many of ``nodes`` a scope covers, for saying how narrow a view is. */
export function scopeNodeCount(scope: Scope, nodes: ClusterNode[], groups: NodeGroup[]) {
  if (scope.kind === 'cluster') return nodes.length
  if (scope.kind === 'group') {
    return groups.find((g) => g.name === scope.name)?.members.length ?? 0
  }
  return 1
}
