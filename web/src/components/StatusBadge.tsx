import type { ContainerStatus } from '../lib/types'

const STYLES: Record<string, { className: string; pulse?: boolean }> = {
  Running: { className: 'badge-ok' },
  Stopped: { className: 'badge-dim' },
  Frozen: { className: 'badge-info' },
  Starting: { className: 'badge-warn', pulse: true },
  Stopping: { className: 'badge-warn', pulse: true },
  Error: { className: 'badge-danger' },
}

export function StatusBadge({ status }: { status: ContainerStatus }) {
  const style = STYLES[status] ?? { className: 'badge-dim' }
  return (
    <span className={`badge ${style.className}`}>
      <span className={`dot${style.pulse ? ' dot-pulse' : ''}`} />
      {status}
    </span>
  )
}
