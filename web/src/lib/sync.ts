import type { AutoSync } from './types'

/**
 * How a save or delete that the cluster keeps level went, as a line to show
 * the user. Empty on a lemondx that is not federated, so nothing in the UI has
 * to know whether there is a cluster before reporting an ordinary save.
 */
export function syncDetail(synced: AutoSync | null | undefined) {
  if (!synced) return undefined
  const verb = synced.deleted ? 'Removed from' : 'Synced to'
  if (synced.ok) return `${verb} ${synced.nodes.join(', ')}.`
  const failed = synced.results.filter((r) => !r.ok)
  return `${verb} ${synced.nodes.length - failed.length} of ${synced.nodes.length} nodes. `
    + `${failed.map((r) => `${r.node}: ${r.error ?? 'failed'}`).join('; ')}. `
    + 'Use Sync on the Nodes tab once they are back.'
}

/** Whether to report it as a success or as something that needs attention. */
export function syncKind(synced: AutoSync | null | undefined) {
  return synced && !synced.ok ? 'error' : 'success'
}
