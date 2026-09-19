import { secondsAgo } from '../lib/format'
import type { AppCheckResult, AppCheckStatus, HealthRecord, HealthStatus } from '../lib/types'

const TONE: Record<HealthStatus, string> = {
  healthy: 'health-ok',
  degraded: 'health-warn',
  unhealthy: 'health-danger',
  starting: 'health-info',
  unknown: 'health-dim',
  paused: 'health-dim',
}

const APP_TONE: Record<AppCheckStatus, string> = {
  ok: 'health-ok',
  warning: 'health-warn',
  critical: 'health-danger',
  unknown: 'health-dim',
  pending: 'health-dim',
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

function appSummary(app: AppCheckResult): string {
  if (!app.configured) return 'App: ok (no app check configured)'
  if (app.status === 'pending') return `App: pending (template ${app.template}; waiting for its first run)`
  const output = app.output ? `: ${app.output}` : ''
  const ran = app.checked_at ? ` (ran ${secondsAgo(app.checked_at)})` : ''
  return `App: ${app.status}${output}${ran}`
}

/**
 * The template's app check, beside the health dot. A diamond rather than a
 * dot so the two read as separate signals at a glance; a template without a
 * check reads ok, never blank.
 */
export function AppDot({ app, onOpen }: { app: AppCheckResult; onOpen?: () => void }) {
  const summary = appSummary(app)
  const dot = `app-dot ${APP_TONE[app.status]}`
  // Only a configured check has output to show.
  if (!onOpen || !app.configured) {
    return <span className={dot} title={summary} role="img" aria-label={summary} />
  }
  return (
    <button type="button" className="app-dot-button" title={`${summary} — click for its output`}
      aria-label={`${summary}. Show its output`}
      // Inside a clickable row: the row would open the drawer as well.
      onClick={(event) => { event.stopPropagation(); onOpen() }}
      onKeyDown={(event) => event.stopPropagation()}>
      <span className={dot} aria-hidden="true" />
    </button>
  )
}

export function AppCheckLabel({ app }: { app: AppCheckResult }) {
  return (
    <span className={`health-label ${APP_TONE[app.status]}`}>
      <span className="app-dot" aria-hidden="true" />
      {app.status}
    </span>
  )
}

/**
 * A running instance its node checks but has not judged yet -- the minute or
 * so after it starts, until the next round. Both markers, grey, so the row
 * does not look like one nobody is watching.
 */
export function PendingHealth() {
  const summary = 'Health: pending (not checked yet)'
  return (
    <>
      <span className="health-dot health-dim" title={summary} role="img" aria-label={summary} />
      <span className="app-dot health-dim" title="App: pending" role="img" aria-label="App: pending" />
    </>
  )
}
