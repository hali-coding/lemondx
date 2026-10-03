import { useEffect, useState } from 'react'
import { api, calls } from '../lib/api'
import type { ConfigureNodeRequest, ConfigureNodeResult, NodeSetupState } from '../lib/types'
import { useAuthInfo } from '../hooks/useAuth'
import { CopyButton } from './CopyButton'
import { Modal } from './Modal'

// auth.MIN_PASSWORD; the server refuses shorter ones anyway.
const MIN_PASSWORD = 8
// How long the server waits before closing its socket, plus room to come back
// up -- before which the new address would only fail to connect.
const RESTART_WAIT_MS = 2500

type TlsMode = 'generate' | 'upload' | 'keep'

interface Props {
  state: NodeSetupState
  onClose: () => void
  /** The server accepted it and is about to go away. */
  onRestarting: () => void
}

/**
 * Leave unconfigured mode: an admin login, a certificate and where to listen,
 * applied together, after which the server restarts itself on the new terms.
 * Nothing on this page works after that, so the last step is sending the
 * browser to the address it now serves.
 */
export function ConfigureNodeDialog({ state, onClose, onRestarting }: Props) {
  const who = useAuthInfo()?.principal?.name
  const needAdmin = state.admins.length === 0
  const [username, setUsername] = useState(needAdmin ? (who ?? 'admin') : '')
  const [password, setPassword] = useState('')
  const [again, setAgain] = useState('')
  const usable = state.certificate && !state.certificate.error ? state.certificate : null
  const [tlsMode, setTlsMode] = useState<TlsMode>(usable ? 'keep' : 'generate')
  const [certHost, setCertHost] = useState(state.suggested_address || 'localhost')
  const [certPem, setCertPem] = useState('')
  const [keyPem, setKeyPem] = useState('')
  const [everywhere, setEverywhere] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [done, setDone] = useState<ConfigureNodeResult | null>(null)

  const adminGiven = username.trim() !== '' || password !== ''
  const adminProblem = !adminGiven && !needAdmin ? ''
    : !username.trim() ? 'Give the admin a user name.'
      : password.length < MIN_PASSWORD ? `Use at least ${MIN_PASSWORD} characters.`
        : password !== again ? 'The passwords do not match.' : ''
  const tlsProblem = tlsMode === 'upload' && (!certPem.trim() || !keyPem.trim())
    ? 'Add both the certificate and its key.' : ''

  const body: ConfigureNodeRequest = {
    ...(adminGiven ? { username: username.trim(), password } : {}),
    tls: tlsMode === 'generate' ? { mode: 'generate', host: certHost.trim() || undefined }
      : tlsMode === 'upload' ? { mode: 'upload', cert: certPem, key: keyPem }
        : { mode: 'keep' },
    host: everywhere ? '0.0.0.0' : '127.0.0.1',
  }

  async function submit(event: React.FormEvent) {
    event.preventDefault()
    if (busy || adminProblem || tlsProblem) return
    setBusy(true)
    setError(null)
    try {
      setDone(await api.configureNode(body))
      onRestarting()
    } catch (cause) {
      setError((cause as Error).message)
      setBusy(false)
    }
  }

  if (done) return <Restarting result={done} state={state} onClose={onClose} />

  return (
    <Modal title="Configure this node"
      subtitle="Turns on logins and HTTPS, and opens the port to other hosts if you want. lemondx restarts to apply it; you then log in at the new address."
      onClose={busy ? () => {} : onClose}
      api={{ ...calls.configureNode(body), secrets: ['password', 'tls.key'] }}
      footer={<>
        <button type="button" className="btn" onClick={onClose} disabled={busy}>Cancel</button>
        <button type="submit" form="configure-form" className="btn btn-primary"
          disabled={busy || !!adminProblem || !!tlsProblem || !!state.blocked}>
          {busy && <span className="spinner" />}Configure and restart
        </button>
      </>}>
      <form id="configure-form" onSubmit={submit} style={{ display: 'grid', gap: 16 }}>
        {state.blocked && (
          <div className="banner banner-warn"><div className="banner-body">
            <h3>Not right now</h3>
            <p>{state.blocked}</p>
          </div></div>
        )}

        <fieldset className="field" style={{ border: 0, padding: 0, margin: 0 }}>
          <label>Admin login</label>
          <span className="hint">
            {needAdmin
              ? 'Logins go on, so somebody has to be able to log in: this account gets admin.'
              : `Admins already here: ${state.admins.join(', ')}. Fill these in to add another, or to set a new password for one of them.`}
          </span>
          <input className="input" aria-label="Admin user name" placeholder="user name"
            value={username} autoComplete="username" disabled={busy} style={{ marginTop: 6 }}
            onChange={(e) => setUsername(e.target.value)} />
          <div className="grid-2">
            <input className="input" type="password" aria-label="Password"
              placeholder="password" value={password} autoComplete="new-password"
              disabled={busy} onChange={(e) => setPassword(e.target.value)} />
            <input className="input" type="password" aria-label="Password again"
              placeholder="password again" value={again} autoComplete="new-password"
              disabled={busy} onChange={(e) => setAgain(e.target.value)} />
          </div>
          {/* Not before a password is typed: the name starts filled in. */}
          {adminProblem && (password || again) && (
            <span className="field-error">{adminProblem}</span>
          )}
        </fieldset>

        <div className="field">
          <label>HTTPS certificate</label>
          <div className="check-list" style={{ maxHeight: 'none' }}>
            {usable && (
              <label className="check" style={{ alignItems: 'flex-start' }}>
                <input type="radio" name="tls-mode" checked={tlsMode === 'keep'} disabled={busy}
                  onChange={() => setTlsMode('keep')} />
                <span>
                  <strong>Keep the one this node already has</strong>
                  <div className="faint mono" style={{ fontSize: 11 }}>
                    {usable.fingerprint_pretty}
                  </div>
                </span>
              </label>
            )}
            <label className="check" style={{ alignItems: 'flex-start' }}>
              <input type="radio" name="tls-mode" checked={tlsMode === 'generate'}
                disabled={busy} onChange={() => setTlsMode('generate')} />
              <span>
                <strong>Generate a self-signed certificate</strong>
                <div className="faint" style={{ fontSize: 12 }}>
                  Browsers warn about it once. Cluster members pin it by fingerprint, so
                  it is all federation needs.
                </div>
              </span>
            </label>
            <label className="check" style={{ alignItems: 'flex-start' }}>
              <input type="radio" name="tls-mode" checked={tlsMode === 'upload'} disabled={busy}
                onChange={() => setTlsMode('upload')} />
              <span>
                <strong>Upload a certificate</strong>
                <div className="faint" style={{ fontSize: 12 }}>
                  One a CA signed, so browsers trust it. PEM, with the key unencrypted.
                </div>
              </span>
            </label>
          </div>
        </div>

        {tlsMode === 'generate' && (
          <div className="field">
            <label htmlFor="configure-cert-host">Name in the certificate</label>
            <input id="configure-cert-host" className="input mono" value={certHost}
              disabled={busy} onChange={(e) => setCertHost(e.target.value)} />
            <span className="hint">The address or DNS name people and other nodes use for this host.</span>
          </div>
        )}

        {tlsMode === 'upload' && (
          <div className="grid-2">
            <PemField id="configure-cert" label="Certificate (chain first)" value={certPem}
              placeholder="-----BEGIN CERTIFICATE-----" disabled={busy} onChange={setCertPem} />
            <PemField id="configure-key" label="Private key" value={keyPem}
              placeholder="-----BEGIN PRIVATE KEY-----" disabled={busy} onChange={setKeyPem} />
          </div>
        )}

        <div className="field">
          <label className="checkbox">
            <input type="checkbox" checked={everywhere} disabled={busy}
              onChange={(e) => setEverywhere(e.target.checked)} />
            Listen on every interface (0.0.0.0)
          </label>
          <span className="hint">
            {everywhere
              ? `Other hosts reach it at https://${state.suggested_address || 'this-host'}:${state.port}, which joining a cluster needs. Logins and HTTPS are on before the port opens.`
              : 'Stays reachable from this host only (an SSH tunnel works), but with logins and HTTPS. A node in a cluster has to be reachable by the others.'}
          </span>
        </div>

        {error && <span className="field-error" role="alert">{error}</span>}
      </form>
    </Modal>
  )
}

/** A PEM text box that a file can fill, since pasting a key is the clumsier way. */
function PemField({ id, label, value, placeholder, disabled, onChange }: {
  id: string
  label: string
  value: string
  placeholder: string
  disabled: boolean
  onChange: (text: string) => void
}) {
  return (
    <div className="field">
      <label htmlFor={id}>{label}</label>
      <textarea id={id} className="input mono" rows={5} value={value} spellCheck={false}
        placeholder={placeholder} disabled={disabled} style={{ fontSize: 11 }}
        onChange={(e) => onChange(e.target.value)} />
      <input type="file" accept=".pem,.crt,.cer,.key,.txt" disabled={disabled}
        aria-label={`${label} from a file`}
        onChange={async (e) => {
          const file = e.target.files?.[0]
          if (file) onChange(await file.text())
        }} />
    </div>
  )
}

/** What happens next, once the server has said yes and is about to go away. */
function Restarting({ result, state, onClose }: {
  result: ConfigureNodeResult
  state: NodeSetupState
  onClose: () => void
}) {
  const [ready, setReady] = useState(false)
  useEffect(() => {
    const timer = window.setTimeout(() => setReady(true), RESTART_WAIT_MS)
    return () => window.clearTimeout(timer)
  }, [])
  // The name and port the browser used, so an SSH tunnel or a forwarded port
  // keeps working -- unless the port itself is what changed.
  const url = result.serving_port === state.port
    ? `${result.scheme}://${window.location.host}`
    : `${result.scheme}://${window.location.hostname}:${result.serving_port}`
  const elsewhere = result.serving_host !== '127.0.0.1' && state.suggested_address
    ? `${result.scheme}://${state.suggested_address}:${result.serving_port}` : ''

  return (
    <Modal title="Restarting"
      subtitle="The configuration is saved, and lemondx is restarting to apply it. This page stops working; carry on at the new address."
      onClose={onClose}
      footer={<button className="btn btn-primary" disabled={!ready}
        onClick={() => window.location.assign(url)}>
        {!ready && <span className="spinner" />}Continue to {url}
      </button>}>
      <div style={{ display: 'grid', gap: 12 }}>
        <p style={{ margin: 0 }}>
          Log in there{result.admin ? <> as <strong>{result.admin}</strong></> : null}.
          {elsewhere && url !== elsewhere && <> Other hosts reach it at <span className="mono">{elsewhere}</span>.</>}
        </p>
        {result.scheme === 'https' && (
          <>
            <p style={{ margin: 0 }}>
              {result.certificate.mode === 'upload'
                ? 'If the browser warns about the certificate, compare its fingerprint with this one:'
                : 'The certificate is self-signed, so the browser warns once. Check that it shows this fingerprint before you accept it:'}
            </p>
            <div className="access-secret">
              <code className="mono" style={{ fontSize: 11 }}>{result.certificate.fingerprint_pretty}</code>
              <CopyButton text={result.certificate.fingerprint_pretty} label="certificate fingerprint" />
            </div>
          </>
        )}
        {result.overridden.length > 0 && (
          <div className="banner banner-warn"><div className="banner-body">
            <h3>Part of this waits for a different command line</h3>
            <ul style={{ margin: 0 }}>
              {result.overridden.map((line) => <li key={line}>{line}</li>)}
            </ul>
          </div></div>
        )}
      </div>
    </Modal>
  )
}
