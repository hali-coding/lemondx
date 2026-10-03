import { bytes } from '../lib/format'
import { CloseIcon } from './Icons'
import type { ImageJob } from '../lib/types'

const JOB_STATES: Record<ImageJob['nodes'][number]['state'], string> = {
  waiting: 'waiting', pulling: 'fetching from the remote', sending: 'sending',
  importing: 'importing',
  done: 'copied', present: 'had it already', failed: 'failed',
}

interface Props {
  job: ImageJob
  /** Say which node the job runs on: where the list spans several. */
  showNode?: boolean
  /** Hide this row; the job itself carries on, and is still reported when it ends. */
  onDismiss?: () => void
}

/** One publish, copy or pinned-build fetch, with each node's progress. */
export function ImageJobRow({ job, showNode = false, onDismiss }: Props) {
  const stage = job.finished_at === null
    ? ({ publishing: 'publishing…', pulling: 'fetching…' }[job.stage as string] ?? 'copying…')
    : job.ok ? 'ready' : 'failed'
  // A snapshot is `instance/snapshot`, shown as just the snapshot on one
  // node; a pinned build is `remote:alias@serial`, shown whole.
  const pinned = job.source?.includes(':') ?? false
  return (
    <div className="list-row">
      <div className="list-row-main">
        <strong className="mono">local:{job.alias}</strong>
        <div className="faint" style={{ fontSize: 12 }}>
          {job.source
            ? `from ${showNode || pinned ? job.source : job.source.split('/')[1]} · ` : ''}
          {showNode ? `on ${job.node} · ` : ''}
          {stage}{job.size ? ` · ${bytes(job.size)}` : ''}
        </div>
        {job.nodes.map((entry) => (
          <div key={entry.node} className="faint" style={{ fontSize: 12 }}>
            {entry.node}: {pinned && entry.node === job.node && entry.state === 'done'
              ? 'fetched from the remote' : JOB_STATES[entry.state]}
            {entry.state === 'sending' && job.size
              ? ` ${Math.floor((entry.sent / job.size) * 100)}% of ${bytes(job.size)}` : ''}
            {entry.error && <span style={{ color: 'var(--danger)' }}> — {entry.error}</span>}
          </div>
        ))}
        {job.error && !job.nodes.some((e) => e.error) && (
          <div style={{ fontSize: 12, color: 'var(--danger)' }}>{job.error}</div>
        )}
      </div>
      {onDismiss && (
        <button className="btn btn-ghost btn-icon" onClick={onDismiss}
          aria-label={`Dismiss local:${job.alias}`} title="Dismiss">
          <CloseIcon size={14} />
        </button>
      )}
    </div>
  )
}
