import { useCallback, useEffect, useState } from 'react'
import { api } from '../lib/api'
import type { ClusterNode, NodeGroup, Scope } from '../lib/types'

const KEY = 'lemondx-scope'

/**
 * How wide the Containers and Templates tabs are looking, shared by both so a
 * template's instances and the container list never disagree about which hosts
 * are in view.
 *
 * Remembered per browser, not on the server: it is how one person is looking at
 * their cluster right now, not a property of the cluster. A remembered scope
 * naming a node that has since gone falls back to local rather than showing an
 * error the user did not cause.
 */
export function useScope() {
  const [scope, setScope] = useState<Scope>({ kind: 'local' })
  const [nodes, setNodes] = useState<ClusterNode[]>([])
  const [groups, setGroups] = useState<NodeGroup[]>([])
  const [loaded, setLoaded] = useState(false)

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
  }, [])

  const choose = useCallback((next: Scope) => {
    setScope(next)
    try {
      window.localStorage.setItem(KEY, JSON.stringify(next))
    } catch { /* private mode: this session only */ }
  }, [])

  // Federation is opt-in, so a lemondx joined to nothing should not grow a
  // control that only ever has one setting.
  const federated = nodes.length > 1
  return { scope: federated ? scope : ({ kind: 'local' } as Scope), choose, nodes, groups,
           federated, loaded }
}

function restore(nodes: ClusterNode[], groups: NodeGroup[]): Scope {
  let stored: unknown
  try {
    stored = JSON.parse(window.localStorage.getItem(KEY) ?? 'null')
  } catch {
    return { kind: 'local' }
  }
  const scope = stored as Scope | null
  if (!scope || typeof scope !== 'object') return { kind: 'local' }
  if (scope.kind === 'local' || scope.kind === 'cluster') return scope
  if (scope.kind === 'node' && nodes.some((n) => n.name === scope.name)) return scope
  if (scope.kind === 'group' && groups.some((g) => g.name === scope.name)) return scope
  return { kind: 'local' }
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
