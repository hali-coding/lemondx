import { useState } from 'react'
import { useCanOperate } from '../hooks/useAuth'
import { api } from '../lib/api'
import { missingSecrets } from '../lib/bootstrap'
import { useBootstrapData } from '../hooks/useBootstrapData'
import type { BootstrapResult, BootstrapSelection } from '../lib/types'
import { BootstrapLog } from './BootstrapLog'
import { BootstrapPicker } from './BootstrapPicker'
import { SshKeyPicker } from './SshKeyPicker'

interface Props {
  name: string
  /** The node it is on, when that is not this one. */
  node?: string
  running: boolean
  onFinished: () => void
}

/** Run bootstrap modules against a container that already exists. */
export function BootstrapPanel({ name, node, running, onFinished }: Props) {
  const canOperate = useCanOperate()
  const { modules, hostKeys, loading } = useBootstrapData(node)
  const [selection, setSelection] = useState<BootstrapSelection>(
    { modules: [], params: {}, ssh_keys: [] })
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState<BootstrapResult | null>(null)
  const [error, setError] = useState<string | null>(null)

  const needsKeys = modules.some(
    (m) => m.uses_ssh_keys && selection.modules.includes(m.id))
  const keysMissing = needsKeys && selection.ssh_keys.length === 0
  const secretsMissing = missingSecrets(modules, selection)

  async function run() {
    setBusy(true)
    setError(null)
    setResult(null)
    try {
      setResult(await api.bootstrap(name, selection, node))
      onFinished()
    } catch (cause) {
      setError((cause as Error).message)
    } finally {
      setBusy(false)
      // Don't leave a password sitting in the form once it has been used.
      setSelection((current) => ({
        ...current,
        params: Object.fromEntries(Object.entries(current.params).filter(
          ([param]) => !modules.some((m) => m.params.some(
            (p) => p.secret && p.name === param)))),
      }))
    }
  }

  if (!running) {
    return (
      <p className="dim" style={{ fontSize: 13 }}>
        Start the container before running bootstrap modules.
      </p>
    )
  }
  if (loading) {
    return <div className="loading-wrap"><span className="spinner" /> Loading modules…</div>
  }

  return (
    <>
      <BootstrapPicker
        modules={modules}
        value={selection}
        onChange={setSelection}
        disabled={busy}
      />

      {needsKeys && (
        <div style={{ marginTop: 12 }}>
          <SshKeyPicker
            hostKeys={hostKeys}
            value={selection.ssh_keys}
            onChange={(keys) => setSelection({ ...selection, ssh_keys: keys })}
            disabled={busy}
          />
        </div>
      )}

      {secretsMissing.length > 0 && (
        <p className="hint" style={{ marginTop: 8, color: 'var(--warn)' }}>
          Enter {secretsMissing.join(' and ')} to run — secrets are never saved.
        </p>
      )}

      <button
        className="btn btn-primary"
        style={{ marginTop: 12 }}
        onClick={run}
        disabled={busy || !canOperate || selection.modules.length === 0 || keysMissing
          || secretsMissing.length > 0}
      >
        {busy && <span className="spinner" />}
        {busy ? 'Running…' : `Run ${selection.modules.length || ''} module${
          selection.modules.length === 1 ? '' : 's'}`}
      </button>

      {error && (
        <div className="banner banner-error" style={{ marginTop: 12 }}>
          <div className="banner-body"><p style={{ margin: 0 }}>{error}</p></div>
        </div>
      )}

      {result && <div style={{ marginTop: 12 }}><BootstrapLog result={result} /></div>}

      <p className="hint" style={{ marginTop: 8 }}>
        Modules are re-runnable: running one again is safe and just reapplies it.
      </p>
    </>
  )
}
