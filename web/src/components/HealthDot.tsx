import { secondsAgo } from '../lib/format'
import type { HealthRecord, HealthStatus } from '../lib/types'

const TONE: Record<HealthStatus, string> = {
  healthy: 'health-ok',
  degraded: 'health-warn',
  unhealthy: 'health-danger',
  starting: 'health-info',
  unknown: 'health-dim',
  paused: 'health-dim',
}

function healthSummary(record: HealthRecord): string {
  const reasons = record.reasons.length ? `: ${record.reasons.join('; ')}` : ''
  return `${record.status[0].toUpperCase()}${record.status.slice(1)}${reasons} (checked ${secondsAgo(record.checked_at)})`
}

/** A small coloured dot for the container list; the title carries the detail. */
export function HealthDot({ record }: { record: HealthRecord }) {
  const summary = healthSummary(record)
  return (
    <span className={`health-dot ${TONE[record.status]}`} title={summary} role="img"
      aria-label={summary} />
  )
}

export function HealthLabel({ record }: { record: HealthRecord }) {
  return (
    <span className={`health-label ${TONE[record.status]}`}>
      <span className="health-dot" aria-hidden="true" />
      {record.status}
    </span>
  )
}
