import { useCallback, useEffect, useState } from 'react'
import { api } from '../lib/api'
import { secondsAgo } from '../lib/format'
import type { AppCheckOutput } from '../lib/types'
import { AppCheckLabel } from './HealthDot'
import { Modal } from './Modal'

interface Props {
  name: string
  /** The node the instance lives on, when that is not this one. */
  node?: string
  onClose: () => void
}

/**
 * The latest run of an instance's app check, with everything it printed.
 * Fetched on open rather than carried in the health poll, which only has the
 * first line; asked of the instance's own node, which is where it ran.
 */
export function AppCheckDialog({ name, node, onClose }: Props) {
  const [data, setData] = useState<AppCheckOutput | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)

  // Starts out loading, so only a refresh has to say so.
  const load = useCallback((signal?: AbortSignal) => {
    api.appCheck(name, node, signal)
      .then((next) => { setData(next); setError(null) })
      .catch((cause) => {
        if ((cause as Error).name !== 'AbortError') setError((cause as Error).message)
      })
      .finally(() => setLoading(false))
  }, [name, node])

  useEffect(() => {
    const controller = new AbortController()
    load(controller.signal)
    return () => controller.abort()
  }, [load])

  const result = data?.result ?? null
  const stdout = result?.stdout.replace(/\n$/, '') ?? ''
  const stderr = result?.stderr.replace(/\n$/, '') ?? ''

  return (
    <Modal
      title={`App check · ${name}`}
      subtitle={data
        ? `From template ${data.template}${node ? ` on ${node}` : ''} · every ${data.interval_seconds}s, timeout ${data.timeout_seconds}s`
        : undefined}
      onClose={onClose}
      footer={
        <>
          <button type="button" className="btn" onClick={() => { setLoading(true); load() }}
            disabled={loading}>
            {loading && <span className="spinner" />}
            Refresh
          </button>
          <button type="button" className="btn btn-primary" onClick={onClose}>Close</button>
        </>
      }
    >
      {error && (
        <div className="banner banner-error" style={{ margin: 0, padding: '10px 12px' }}>
          <div className="banner-body">
            <p style={{ margin: 0, color: 'var(--danger)' }}>{error}</p>
          </div>
        </div>
      )}

      {!!data?.missed_rounds && (
        <div className="banner banner-warn" style={{ margin: 0, padding: '10px 12px' }}>
          <div className="banner-body">
            <p style={{ margin: 0 }}>
              The last health round did not see {name} running, so its check is paused and
              this result may be out of date. It resumes when a round sees it again.
            </p>
          </div>
        </div>
      )}

      {data && !result && (
        <p className="hint">The check has not finished its first run yet. Refresh in a moment.</p>
      )}

      {result && (
        <>
          <div className="app-check">
            <AppCheckLabel app={{ ...result, configured: true, template: data!.template }} />
            <span className="faint" style={{ fontSize: 12.5 }}>
              {result.code === null ? 'did not run' : `exit ${result.code}`}
              {result.ms !== null ? ` · ${result.ms} ms` : ''}
              {result.checked_at ? ` · ran ${secondsAgo(result.checked_at)}` : ''}
            </span>
          </div>
          <div className="console app-check-output">
            {result.truncated && <div className="dim">… earlier output cut</div>}
            {stdout && <div>{stdout}</div>}
            {stderr && <div className="err">{stderr}</div>}
            {!stdout && !stderr && <div className="dim">The script printed nothing.</div>}
          </div>
        </>
      )}

      {data && (
        <details className="bootstrap-section">
          <summary>Script</summary>
          <div className="console app-check-output">{data.script.replace(/\n$/, '')}</div>
        </details>
      )}
    </Modal>
  )
}
