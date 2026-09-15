import { useCallback, useEffect, useState } from 'react'
import { useAuthInfo } from '../hooks/useAuth'
import { api } from '../lib/api'
import type { ApiToken, CreatedApiToken, LocalUser, Role } from '../lib/types'
import { ConfirmDialog } from './ConfirmDialog'
import { CopyButton } from './CopyButton'
import { PlusIcon, TrashIcon } from './Icons'
import { Modal } from './Modal'

interface Props {
  onNotify: (kind: 'success' | 'error' | 'info', title: string, detail?: string) => void
}

const EXPIRY_CHOICES: { label: string; days: number | null }[] = [
  { label: 'Never', days: null },
  { label: '1 day', days: 1 },
  { label: '7 days', days: 7 },
  { label: '30 days', days: 30 },
  { label: '90 days', days: 90 },
]

function when(epoch: number | null) {
  return epoch ? new Date(epoch * 1000).toLocaleString() : '—'
}

/** API tokens for scripts, and for admins the local user list. */
export function AccessView({ onNotify }: Props) {
  const info = useAuthInfo()
  const principal = info?.principal
  if (info?.enabled && !principal) return null      // behind the login gate
  const isAdmin = !principal || principal.role === 'admin'
  // The server will not let a token manage tokens, so say so up front.
  const viaToken = !!principal && (principal.via.startsWith('token:') || principal.via === 'static-token')

  return (
    <>
      <div className="section-head">
        <h2>Access</h2>
        <span className="faint" style={{ fontSize: 12.5 }}>
          {principal && info?.enabled
            ? `signed in as ${principal.name} (${principal.role})`
            : 'API tokens and users'}
        </span>
      </div>

      {info && !info.enabled && (
        <div className="banner">
          <div className="banner-body">
            <h3>Authentication is off</h3>
            <p>
              Anyone who can reach this server has full access. Tokens and users made here are
              stored now and take effect once lemondx is started with <code>--auth</code> — see
              docs/security.md.
            </p>
          </div>
        </div>
      )}

      {viaToken ? (
        <div className="card"><div className="empty">
          <h3>Signed in with an API token</h3>
          <p>A token cannot create or revoke tokens. Log in with a password, or use
            <code> lemondx tokens</code> on the host.</p>
        </div></div>
      ) : (
        <TokensCard onNotify={onNotify} canGrantAdmin={isAdmin} showOwner={isAdmin} />
      )}

      {isAdmin && !viaToken && <UsersCard onNotify={onNotify} localEnabled={!!info?.methods.includes('local')} />}
    </>
  )
}

function TokensCard({ onNotify, canGrantAdmin, showOwner }: Props & {
  canGrantAdmin: boolean
  showOwner: boolean
}) {
  const [tokens, setTokens] = useState<ApiToken[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [name, setName] = useState('')
  const [role, setRole] = useState<Role>('read')
  const [expiry, setExpiry] = useState(3)
  const [busy, setBusy] = useState(false)
  const [created, setCreated] = useState<CreatedApiToken | null>(null)
  const [pendingRevoke, setPendingRevoke] = useState<ApiToken | null>(null)

  const load = useCallback((signal?: AbortSignal) => api.apiTokens(signal)
    .then((loaded) => { setTokens(loaded); setError(null) })
    .catch((cause) => {
      if ((cause as Error).name !== 'AbortError') setError((cause as Error).message)
    }), [])

  useEffect(() => {
    const controller = new AbortController()
    load(controller.signal)
    return () => controller.abort()
  }, [load])

  async function create(event: React.FormEvent) {
    event.preventDefault()
    setBusy(true)
    try {
      const token = await api.createApiToken({
        name: name.trim(), role, expires_days: EXPIRY_CHOICES[expiry].days,
      })
      setCreated(token)
      setName('')
      await load()
    } catch (cause) {
      onNotify('error', 'Could not create the token', (cause as Error).message)
    } finally {
      setBusy(false)
    }
  }

  async function revoke() {
    if (!pendingRevoke) return
    setBusy(true)
    try {
      await api.revokeApiToken(pendingRevoke.id)
      onNotify('success', `Revoked ${pendingRevoke.name}`)
      await load()
    } catch (cause) {
      onNotify('error', 'Could not revoke the token', (cause as Error).message)
    } finally {
      setBusy(false)
      setPendingRevoke(null)
    }
  }

  return (
    <section className="card access-card">
      <header className="access-head">
        <h3>API tokens</h3>
        <span className="faint">Send as <code>Authorization: Bearer &lt;token&gt;</code></span>
      </header>

      <form className="access-form" onSubmit={create}>
        <div className="field">
          <label htmlFor="token-name">Name</label>
          <input id="token-name" className="input" value={name} maxLength={64}
            placeholder="ci-deploy" onChange={(event) => setName(event.target.value)} />
        </div>
        <div className="field">
          <label htmlFor="token-role">Access</label>
          <select id="token-role" className="select" value={role}
            onChange={(event) => setRole(event.target.value as Role)}>
            <option value="read">Read-only</option>
            {canGrantAdmin && <option value="admin">Admin</option>}
          </select>
        </div>
        <div className="field">
          <label htmlFor="token-expiry">Expires</label>
          <select id="token-expiry" className="select" value={expiry}
            onChange={(event) => setExpiry(Number(event.target.value))}>
            {EXPIRY_CHOICES.map((choice, index) => (
              <option key={choice.label} value={index}>{choice.label}</option>
            ))}
          </select>
        </div>
        <button className="btn btn-primary" disabled={busy || !name.trim()}>
          <PlusIcon /> Create token
        </button>
      </form>

      {error && <div className="banner banner-error access-inset"><div className="banner-body"><p>{error}</p></div></div>}

      {tokens === null && !error ? (
        <div className="loading-wrap"><span className="spinner" /></div>
      ) : tokens && tokens.length === 0 ? (
        <div className="empty"><p>No tokens yet.</p></div>
      ) : tokens && (
        <div className="table-scroll">
          <table className="ctable">
            <thead><tr>
              <th>Name</th>{showOwner && <th>Owner</th>}<th>Access</th><th>Created</th>
              <th>Expires</th><th>Last used</th><th aria-label="Actions" />
            </tr></thead>
            <tbody>{tokens.map((token) => (
              <tr key={token.id}>
                <td><strong>{token.name}</strong><div className="res-sub mono">{token.id}</div></td>
                {showOwner && <td>{token.owner}</td>}
                <td><span className={`badge ${token.role === 'admin' ? 'badge-warn' : 'badge-dim'}`}>
                  {token.role}</span></td>
                <td className="dim">{when(token.created)}</td>
                <td>{token.expired
                  ? <span className="badge badge-danger">expired</span>
                  : <span className="dim">{token.expires ? when(token.expires) : 'never'}</span>}</td>
                <td className="dim">{when(token.last_used)}</td>
                <td>
                  <div className="row-actions">
                    <button className="btn btn-sm btn-icon btn-danger" disabled={busy}
                      aria-label={`Revoke token ${token.name}`} title="Revoke"
                      onClick={() => setPendingRevoke(token)}>
                      <TrashIcon />
                    </button>
                  </div>
                </td>
              </tr>
            ))}</tbody>
          </table>
        </div>
      )}

      {created && (
        <Modal title={`Token “${created.name}” created`}
          subtitle="Copy it now. It is not stored anywhere it can be shown again."
          onClose={() => setCreated(null)}
          footer={<button className="btn btn-primary" onClick={() => setCreated(null)}>Done</button>}>
          <div className="access-secret">
            <code className="mono">{created.token}</code>
            <CopyButton text={created.token} label={`token ${created.name}`} />
          </div>
        </Modal>
      )}

      {pendingRevoke && (
        <ConfirmDialog
          title={`Revoke ${pendingRevoke.name}?`}
          message="Anything using this token stops working immediately. This cannot be undone."
          confirmLabel="Revoke"
          danger
          busy={busy}
          onConfirm={revoke}
          onCancel={() => setPendingRevoke(null)}
        />
      )}
    </section>
  )
}

function UsersCard({ onNotify, localEnabled }: Props & { localEnabled: boolean }) {
  const [users, setUsers] = useState<LocalUser[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [editing, setEditing] = useState<LocalUser | 'new' | null>(null)
  const [pendingRemove, setPendingRemove] = useState<string | null>(null)

  const load = useCallback((signal?: AbortSignal) => api.users(signal)
    .then((loaded) => { setUsers(loaded); setError(null) })
    .catch((cause) => {
      if ((cause as Error).name !== 'AbortError') setError((cause as Error).message)
    }), [])

  useEffect(() => {
    const controller = new AbortController()
    load(controller.signal)
    return () => controller.abort()
  }, [load])

  async function changeRole(user: LocalUser, role: Role) {
    setBusy(true)
    try {
      await api.saveUser(user.name, { role })
      onNotify('success', `${user.name} is now ${role === 'admin' ? 'an admin' : 'read-only'}`)
      await load()
    } catch (cause) {
      onNotify('error', `Could not change ${user.name}`, (cause as Error).message)
    } finally {
      setBusy(false)
    }
  }

  async function remove() {
    if (!pendingRemove) return
    setBusy(true)
    try {
      await api.removeUser(pendingRemove)
      onNotify('success', `Removed ${pendingRemove}`)
      await load()
    } catch (cause) {
      onNotify('error', `Could not remove ${pendingRemove}`, (cause as Error).message)
    } finally {
      setBusy(false)
      setPendingRemove(null)
    }
  }

  return (
    <section className="card access-card">
      <header className="access-head">
        <h3>Local users</h3>
        <span className="faint">
          {localEnabled
            ? 'Accounts kept by lemondx itself, separate from the host’s.'
            : 'Not used until lemondx is started with --auth local.'}
        </span>
        <button className="btn btn-sm btn-primary access-head-end" onClick={() => setEditing('new')}>
          <PlusIcon /> Add user
        </button>
      </header>

      {error && <div className="banner banner-error access-inset"><div className="banner-body"><p>{error}</p></div></div>}

      {users === null && !error ? (
        <div className="loading-wrap"><span className="spinner" /></div>
      ) : users && users.length === 0 ? (
        <div className="empty"><p>No local users.</p></div>
      ) : users && (
        <div className="table-scroll">
          <table className="ctable">
            <thead><tr><th>Name</th><th>Access</th><th>Created</th><th>Changed</th>
              <th aria-label="Actions" /></tr></thead>
            <tbody>{users.map((user) => (
              <tr key={user.name}>
                <td><strong>{user.name}</strong></td>
                <td>
                  <select className="select" value={user.role} disabled={busy}
                    aria-label={`Access for ${user.name}`}
                    onChange={(event) => changeRole(user, event.target.value as Role)}>
                    <option value="read">Read-only</option>
                    <option value="admin">Admin</option>
                  </select>
                </td>
                <td className="dim">{when(user.created)}</td>
                <td className="dim">{when(user.updated)}</td>
                <td>
                  <div className="row-actions">
                    <button className="btn btn-sm" disabled={busy}
                      onClick={() => setEditing(user)}>Set password</button>
                    <button className="btn btn-sm btn-icon btn-danger" disabled={busy}
                      aria-label={`Remove user ${user.name}`} title="Remove"
                      onClick={() => setPendingRemove(user.name)}>
                      <TrashIcon />
                    </button>
                  </div>
                </td>
              </tr>
            ))}</tbody>
          </table>
        </div>
      )}

      {editing && (
        <UserDialog user={editing === 'new' ? null : editing}
          onCancel={() => setEditing(null)}
          onSaved={(message) => { setEditing(null); onNotify('success', message); load() }} />
      )}

      {pendingRemove && (
        <ConfirmDialog
          title={`Remove ${pendingRemove}?`}
          message="Their sessions end immediately. API tokens they created keep working until revoked."
          confirmLabel="Remove"
          danger
          busy={busy}
          onConfirm={remove}
          onCancel={() => setPendingRemove(null)}
        />
      )}
    </section>
  )
}

function UserDialog({ user, onCancel, onSaved }: {
  user: LocalUser | null
  onCancel: () => void
  onSaved: (message: string) => void
}) {
  const [name, setName] = useState(user?.name ?? '')
  const [role, setRole] = useState<Role>(user?.role ?? 'read')
  const [password, setPassword] = useState('')
  const [again, setAgain] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const mismatch = again.length > 0 && again !== password
  const canSave = !!name.trim() && password.length >= 8 && password === again && !busy

  async function save(event: React.FormEvent) {
    event.preventDefault()
    if (!canSave) return
    setBusy(true)
    setError(null)
    try {
      await api.saveUser(name.trim(), user ? { password } : { password, role })
      onSaved(user ? `Changed the password for ${user.name}` : `Added ${name.trim()}`)
    } catch (cause) {
      setError((cause as Error).message)
      setBusy(false)
    }
  }

  return (
    <Modal title={user ? `Set password for ${user.name}` : 'Add local user'}
      subtitle={user ? 'Their current sessions end when the password changes.' : undefined}
      onClose={busy ? () => {} : onCancel}
      footer={<>
        <button type="button" className="btn" onClick={onCancel} disabled={busy}>Cancel</button>
        <button type="submit" form="user-form" className="btn btn-primary" disabled={!canSave}>
          {busy && <span className="spinner" />}{user ? 'Set password' : 'Add user'}
        </button>
      </>}>
      <form id="user-form" onSubmit={save} style={{ display: 'grid', gap: 14 }}>
        {!user && (
          <div className="grid-2">
            <div className="field">
              <label htmlFor="user-name">Name</label>
              <input id="user-name" className="input" value={name} autoComplete="off"
                autoCapitalize="off" spellCheck={false} maxLength={64}
                onChange={(event) => setName(event.target.value)} />
            </div>
            <div className="field">
              <label htmlFor="user-role">Access</label>
              <select id="user-role" className="select" value={role}
                onChange={(event) => setRole(event.target.value as Role)}>
                <option value="read">Read-only</option>
                <option value="admin">Admin</option>
              </select>
            </div>
          </div>
        )}
        <div className="grid-2">
          <div className="field">
            <label htmlFor="user-password">Password</label>
            <input id="user-password" className="input" type="password" value={password}
              autoComplete="new-password" onChange={(event) => setPassword(event.target.value)} />
            <span className="hint">At least 8 characters.</span>
          </div>
          <div className="field">
            <label htmlFor="user-again">Again</label>
            <input id="user-again" className="input" type="password" value={again}
              autoComplete="new-password" onChange={(event) => setAgain(event.target.value)} />
            {mismatch && <span className="field-error">Does not match.</span>}
          </div>
        </div>
        {error && <span className="field-error" role="alert">{error}</span>}
      </form>
    </Modal>
  )
}
