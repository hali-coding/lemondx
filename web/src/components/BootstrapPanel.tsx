import { useState } from 'react'
import { api } from '../lib/api'
import { useBootstrapData } from '../hooks/useBootstrapData'
import type { BootstrapResult, BootstrapSelection } from '../lib/types'
import { BootstrapLog } from './BootstrapLog'
import { BootstrapPicker } from './BootstrapPicker'
import { SshKeyPicker } from './SshKeyPicker'

interface Props {
  name: string
  running: boolean
  onFinished: () => void
}

/** Run bootstrap modules against a container that already exists. */
export function BootstrapPanel({ name, running, onFinished }: Props) {
  const { modules, hostKeys, loading } = useBootstrapData()
  const [selection, setSelection] = useState<BootstrapSelection>(
    { modules: [], params: {}, ssh_keys: [] })
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState<BootstrapResult | null>(null)
  const [error, setError] = useState<string | null>(null)

  const needsKeys = modules.some(
    (m) => m.uses_ssh_keys && selection.modules.includes(m.id))
  const keysMissing = needsKeys && selection.ssh_keys.length === 0

  // Keys without a listening sshd is a dead end, so say so -- softly, since
  // exec-only containers and externally-managed sshd are both legitimate.
  const SSH_SERVER = 'ssh-server'
  const hasSshServerModule = modules.some((m) => m.id === SSH_SERVER)
  const noSshServer = needsKeys && hasSshServerModule
    && !selection.modules.includes(SSH_SERVER)

  async function run() {
    setBusy(true)
    setError(null)
    setResult(null)
    try {
      setResult(await api.bootstrap(name, selection))
      onFinished()
    } catch (cause) {
      setError((cause as Error).message)
    } finally {
      setBusy(false)
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

      {noSshServer && !keysMissing && (
        <p className="hint" style={{ marginTop: 8 }}>
          Keys will be installed, but nothing will be listening — add the{' '}
          <strong>SSH server</strong> module if you want to ssh in.
        </p>
      )}

      <button
        className="btn btn-primary"
        style={{ marginTop: 12 }}
        onClick={run}
        disabled={busy || selection.modules.length === 0 || keysMissing}
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
