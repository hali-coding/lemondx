import { useState } from 'react'
import { api, setToken } from '../lib/api'
import type { AuthInfo } from '../lib/types'
import { Modal } from './Modal'

interface Props {
  info: AuthInfo
  onDone: (info: AuthInfo | null) => void
}

/**
 * Shown when the API answers 401. Offers whatever the server accepts: a
 * password when --auth local/pam is on, a pasted API token when tokens are,
 * and otherwise explains that the proxy in front is what signs people in.
 */
export function LoginGate({ info, onDone }: Props) {
  const acceptsTokens = info.methods.includes('token')
  const [mode, setMode] = useState<'password' | 'token'>(
    info.password_login ? 'password' : 'token')
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [token, setTokenValue] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  if (!info.password_login && !acceptsTokens) {
    return (
      <Modal title="Sign in required" onClose={() => {}}
        footer={<button className="btn btn-primary" onClick={() => window.location.reload()}>
          Reload</button>}>
        <p style={{ margin: 0 }}>
          This server takes your identity from the reverse proxy in front of it, and the proxy
          did not vouch for this request. Open lemondx through the proxy&apos;s address and sign
          in there.
        </p>
      </Modal>
    )
  }

  async function submit(event: React.FormEvent) {
    event.preventDefault()
    setError(null)
    if (mode === 'token') {
      if (!token.trim()) return
      setToken(token.trim())
      onDone(null)
      return
    }
    setBusy(true)
    try {
      // A stale pasted token would be sent alongside the new cookie, and the
      // server believes the header first.
      setToken(null)
      onDone(await api.login(username.trim(), password))
    } catch (cause) {
      setError((cause as Error).message)
      setPassword('')
    } finally {
      setBusy(false)
    }
  }

  const canSubmit = mode === 'token' ? !!token.trim() : !!username.trim() && !!password && !busy

  return (
    <Modal
      title={mode === 'password' ? 'Log in to lemondx' : 'API token required'}
      subtitle={mode === 'password'
        ? (info.methods.includes('pam') ? 'Use your account on this host.' : undefined)
        : 'Paste an API token to continue.'}
      onClose={() => {}}
      footer={
        <>
          {info.password_login && acceptsTokens && (
            <button type="button" className="btn btn-ghost" style={{ marginRight: 'auto' }}
              onClick={() => { setMode(mode === 'password' ? 'token' : 'password'); setError(null) }}>
              {mode === 'password' ? 'Use an API token' : 'Log in with a password'}
            </button>
          )}
          <button type="submit" form="login-form" className="btn btn-primary" disabled={!canSubmit}>
            {busy && <span className="spinner" />}
            {mode === 'password' ? 'Log in' : 'Unlock'}
          </button>
        </>
      }
    >
      <form id="login-form" onSubmit={submit} style={{ display: 'grid', gap: 14 }}>
        {mode === 'password' ? (
          <>
            <div className="field">
              <label htmlFor="login-user">Username</label>
              <input id="login-user" className="input" value={username} autoComplete="username"
                autoCapitalize="off" spellCheck={false}
                onChange={(event) => setUsername(event.target.value)} />
            </div>
            <div className="field">
              <label htmlFor="login-password">Password</label>
              <input id="login-password" className="input" type="password" value={password}
                autoComplete="current-password"
                onChange={(event) => setPassword(event.target.value)} />
            </div>
          </>
        ) : (
          <div className="field">
            <label htmlFor="token-input">Token</label>
            <input id="token-input" className="input mono" type="password" value={token}
              autoComplete="off" spellCheck={false}
              onChange={(event) => setTokenValue(event.target.value)} />
            <span className="hint">Kept for this browser tab only.</span>
          </div>
        )}
        {error && <span className="field-error" role="alert">{error}</span>}
      </form>
    </Modal>
  )
}
