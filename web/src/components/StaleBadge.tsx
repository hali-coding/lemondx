import { staleSummary } from '../lib/stale'
import type { StaleKind } from '../lib/types'

/** A small "stale" marker, with what changed and what to do in its title. */
export function StaleBadge({ stale, template, stack }: {
  stale: StaleKind[]
  template?: string | null
  stack?: string | null
}) {
  if (!stale.length) return null
  const summary = staleSummary(stale, template, stack)
  return (
    <span className="badge badge-warn stale-badge" title={summary} aria-label={summary}>
      stale
    </span>
  )
}
