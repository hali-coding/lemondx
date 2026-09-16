import { useCallback, useEffect, useState } from 'react'
import { useCanWrite } from '../hooks/useAuth'
import { api } from '../lib/api'
import type {
  ClusterInfo, ClusterNode, ClusterNodeDetail, JoinCode, NodeGroup, SyncKind, SyncResult,
} from '../lib/types'
import { ConfirmDialog } from './ConfirmDialog'
import { CopyButton } from './CopyButton'
import { KeyIcon, PlusIcon, RefreshIcon, ServerIcon, TrashIcon, UploadIcon } from './Icons'
import { Modal } from './Modal'

interface Props {
  onNotify: (kind: 'success' | 'error' | 'info', title: string, detail?: string) => void
}

// Reachability comes from the server's own probe, which it caches for a few
// seconds; polling faster than that would only re-read the same answer.
const POLL_INTERVAL = 10000

const SYNC_KINDS: { id: SyncKind; label: string; hint: string }[] = [
  { id: 'templates', label: 'Templates', hint: 'with the uploaded modules they use' },
  { id: 'modules', label: 'Modules', hint: 'uploaded ones; built-ins are everywhere already' },
  { id: 'profiles', label: 'Bootstrap profiles', hint: 'named module selections' },
]

function when(epoch: number | null | undefined) {
  return epoch ? new Date(epoch * 1000).toLocaleString() : '—'
}

function short(fingerprint: string) {
  return fingerprint ? `${fingerprint.slice(0, 8)}…${fingerprint.slice(-8)}` : '—'
}

/** Nodes this lemondx federates with, the groups they form, and what spans them. */
export function NodesView({ onNotify }: Props) {
  const canWrite = useCanWrite()
  const [info, setInfo] = useState<ClusterInfo | null>(null)
  const [nodes, setNodes] = useState<ClusterNode[] | null>(null)
  const [groups, setGroups] = useState<NodeGroup[]>([])
  const [error, setError] = useState<string | null>(null)
  const [dialog, setDialog] = useState<'join' | 'invite' | 'sync' | null>(null)
  const [editingGroup, setEditingGroup] = useState<NodeGroup | 'new' | null>(null)
  const [pendingRemove, setPendingRemove] = useState<ClusterNode | null>(null)
  const [busy, setBusy] = useState(false)

  const load = useCallback(async (signal?: AbortSignal) => {
    try {
      const [nextInfo, nextNodes, nextGroups] = await Promise.all([
        api.cluster(signal), api.nodes(signal), api.nodeGroups(signal),
      ])
      setInfo(nextInfo)
      setNodes(nextNodes)
      setGroups(nextGroups)
      setError(null)
    } catch (cause) {
      if ((cause as Error).name === 'AbortError') return
      setError((cause as Error).message)
    }
  }, [])

  useEffect(() => {
    const controller = new AbortController()
    // oxlint-disable-next-line react/set-state-in-effect -- async fetch, not a sync setState
    load(controller.signal)
    const timer = window.setInterval(() => load(), POLL_INTERVAL)
    return () => {
      controller.abort()
      window.clearInterval(timer)
    }
  }, [load])

  async function removeNode() {
    if (!pendingRemove) return
    setBusy(true)
    try {
      const result = await api.removeNode(pendingRemove.name)
      onNotify('success', `Removed ${pendingRemove.name}`, result.still_holds_credential
        ? `It still holds the cluster credential — run \`lemondx cluster rotate\` to cut it off.`
        : undefined)
      await load()
    } catch (cause) {
      onNotify('error', `Could not remove ${pendingRemove.name}`, (cause as Error).message)
    } finally {
      setBusy(false)
      setPendingRemove(null)
    }
  }

  async function deleteGroup(name: string) {
    try {
      await api.deleteNodeGroup(name)
      onNotify('success', `Deleted group ${name}`)
      await load()
    } catch (cause) {
      onNotify('error', `Could not delete ${name}`, (cause as Error).message)
    }
  }

  const peers = (nodes ?? []).filter((node) => !node.self)

  return (
    <>
      <div className="section-head">
        <h2>Nodes</h2>
        <span className="faint" style={{ fontSize: 12.5 }}>
          this host and the lemondx nodes it federates with
        </span>
      </div>

      {error && (
        <div className="banner banner-error">
          <div className="banner-body"><h3>Could not read the cluster</h3><p>{error}</p></div>
        </div>
      )}

      {info && <ThisNode info={info} peers={peers.length} />}

      <section className="card access-card">
        <header className="access-head">
          <h3>Nodes</h3>
          <span className="faint">
            Every node runs its own lemondx; this one only knows how to call them.
          </span>
          <div className="row-actions access-head-end">
            <button className="btn btn-sm btn-icon" onClick={() => load()} title="Refresh"
              aria-label="Refresh nodes">
              <RefreshIcon />
            </button>
            <button className="btn btn-sm" disabled={!canWrite || peers.length === 0}
              onClick={() => setDialog('sync')} title={peers.length === 0
                ? 'Nothing to sync to yet' : 'Copy templates and modules to other nodes'}>
              <UploadIcon /> Sync
            </button>
            <button className="btn btn-sm" disabled={!canWrite}
              onClick={() => setDialog('invite')}
              title="Make a code for another host to join this cluster with">
              <KeyIcon /> Invite new node
            </button>
            <button className="btn btn-sm btn-primary" disabled={!canWrite}
              onClick={() => setDialog('join')}
              title="Paste a code from another node to make this host a member">
              <PlusIcon /> Join cluster
            </button>
          </div>
        </header>

        {nodes === null && !error ? (
          <div className="loading-wrap"><span className="spinner" /></div>
        ) : (
          <div className="node-grid">
            {(nodes ?? []).map((node) => (
              <NodeCard key={node.name} node={node} canWrite={canWrite}
                onRemove={() => setPendingRemove(node)} onNotify={onNotify} />
            ))}
          </div>
        )}

        {peers.length === 0 && nodes !== null && (
          <div className="empty">
            <h3>No other nodes yet</h3>
            <p>
              Press <strong>Invite new node</strong> here (or run <code>lemondx cluster
              invite</code>) and redeem the code it prints with <strong>Join
              cluster</strong> on the other host. Everything already works on this host
              alone.
            </p>
          </div>
        )}
      </section>

      <section className="card access-card">
        <header className="access-head">
          <h3>Node groups</h3>
          <span className="faint">A name for a set of nodes, to launch or sync across at once.</span>
          <button className="btn btn-sm btn-primary access-head-end" disabled={!canWrite}
            onClick={() => setEditingGroup('new')}>
            <PlusIcon /> New group
          </button>
        </header>

        {groups.length === 0 ? (
          <div className="empty"><p>No groups. Without one, a launch names its nodes directly.</p></div>
        ) : (
          <div className="table-scroll">
            <table className="ctable">
              <thead><tr><th>Group</th><th>Members</th><th>Description</th>
                <th aria-label="Actions" /></tr></thead>
              <tbody>{groups.map((group) => (
                <tr key={group.name}>
                  <td><strong>{group.name}</strong></td>
                  <td>
                    {group.members.length === 0 ? <span className="dim">none</span>
                      : group.members.map((member) => (
                        <span key={member}
                          className={`badge ${group.unknown_members.includes(member)
                            ? 'badge-danger' : 'badge-dim'}`}
                          title={group.unknown_members.includes(member)
                            ? 'Not a node here — removed, or never enrolled' : undefined}
                          style={{ marginRight: 4 }}>
                          {member}
                        </span>
                      ))}
                  </td>
                  <td className="dim">{group.description || '—'}</td>
                  <td>
                    <div className="row-actions">
                      <button className="btn btn-sm" disabled={!canWrite}
                        onClick={() => setEditingGroup(group)}>Edit</button>
                      <button className="btn btn-sm btn-icon btn-danger" disabled={!canWrite}
                        aria-label={`Delete group ${group.name}`} title="Delete"
                        onClick={() => deleteGroup(group.name)}>
                        <TrashIcon />
                      </button>
                    </div>
                  </td>
                </tr>
              ))}</tbody>
            </table>
          </div>
        )}
      </section>

      {dialog === 'join' && (
        <JoinDialog onCancel={() => setDialog(null)}
          onDone={(message, detail) => {
            setDialog(null)
            onNotify('success', message, detail)
            load()
          }} />
      )}

      {dialog === 'invite' && (
        <InviteDialog info={info} onClose={() => { setDialog(null); load() }} />
      )}

      {dialog === 'sync' && (
        <SyncDialog nodes={peers} groups={groups} onCancel={() => setDialog(null)}
          onNotify={onNotify} />
      )}

      {editingGroup && (
        <GroupDialog group={editingGroup === 'new' ? null : editingGroup}
          nodes={nodes ?? []}
          onCancel={() => setEditingGroup(null)}
          onSaved={(message) => {
            setEditingGroup(null)
            onNotify('success', message)
            load()
          }} />
      )}

      {pendingRemove && (
        <ConfirmDialog
          title={`Remove ${pendingRemove.name}?`}
          message={`Every member is told to drop ${pendingRemove.name}. Its instances are untouched. Because the cluster credential is shared, this does not by itself stop it calling in — run \`lemondx cluster rotate\` afterwards for that.`}
          confirmLabel="Remove"
          danger
          busy={busy}
          onConfirm={removeNode}
          onCancel={() => setPendingRemove(null)}
        />
      )}
    </>
  )
}

/** What this node looks like to the others, and whether anything can join it. */
function ThisNode({ info, peers }: { info: ClusterInfo; peers: number }) {
  return (
    <section className="card node-self">
      <div className="node-self-main">
        <h3><ServerIcon size={16} /> {info.node.name} <span className="badge badge-dim">this node</span></h3>
        <dl className="node-facts">
          <div><dt>Address</dt><dd>{info.node.url || <span className="dim">not advertised</span>}</dd></div>
          <div>
            <dt>Certificate</dt>
            <dd className="mono" title={info.fingerprint_pretty || undefined}>
              {info.node.fingerprint ? short(info.node.fingerprint) : <span className="dim">none</span>}
              {info.node.fingerprint && (
                <CopyButton text={info.fingerprint_pretty} label="certificate fingerprint" />
              )}
            </dd>
          </div>
          <div>
            <dt>Cluster</dt>
            <dd>{info.in_cluster ? `${peers + 1} nodes` : 'not in a cluster'}</dd>
          </div>
        </dl>
      </div>
      {info.reachable_because && (
        <div className="banner banner-warn node-self-note">
          <div className="banner-body">
            <h3>Other nodes could not reach this one</h3>
            <p>{info.reachable_because}</p>
          </div>
        </div>
      )}
      {info.remote_requires_token && (
        <div className="banner node-self-note">
          <div className="banner-body">
            <h3>Requests from other hosts need an API token</h3>
            <p>
              Authentication is off here, and a cluster member cannot treat whoever reaches
              its port as an admin. Peers use the cluster credential; anyone else needs a
              token. Browsing from this host is unaffected.
            </p>
          </div>
        </div>
      )}
    </section>
  )
}

function NodeCard({ node, canWrite, onRemove, onNotify }: {
  node: ClusterNode
  canWrite: boolean
  onRemove: () => void
  onNotify: Props['onNotify']
}) {
  const [detail, setDetail] = useState<ClusterNodeDetail | null>(null)
  const [open, setOpen] = useState(false)
  const state = node.state
  const status = !state ? 'unknown' : state.reachable ? (state.ready ? 'ok' : 'not set up') : 'unreachable'

  async function toggle() {
    const next = !open
    setOpen(next)
    if (!next || detail) return
    try {
      setDetail(await api.node(node.name))
    } catch (cause) {
      onNotify('error', `Could not read ${node.name}`, (cause as Error).message)
      setOpen(false)
    }
  }

  return (
    <article className={`node-card${state && !state.reachable ? ' node-card-down' : ''}`}>
      <header>
        <h4>
          <span className={`node-dot node-dot-${status === 'ok' ? 'ok'
            : status === 'unreachable' ? 'down' : 'warn'}`} aria-hidden />
          {node.name}
          {node.self && <span className="badge badge-dim">this node</span>}
        </h4>
        {canWrite && !node.self && (
          <button className="btn btn-sm btn-icon btn-danger" onClick={onRemove}
            aria-label={`Remove node ${node.name}`} title="Remove">
            <TrashIcon />
          </button>
        )}
      </header>

      <p className="node-url mono">{node.url || <span className="dim">local</span>}</p>

      {state?.error && <p className="node-error">{state.error}</p>}

      <dl className="node-facts">
        <div><dt>Daemon</dt><dd>{state?.product ?? '—'} {state?.server_version ?? ''}</dd></div>
        <div><dt>Instances</dt><dd>{state ? `${state.containers} (${state.running} running)` : '—'}</dd></div>
        {!node.self && (
          <div>
            <dt>Certificate</dt>
            <dd className="mono">{short(node.fingerprint)}</dd>
          </div>
        )}
        {!node.self && <div><dt>Added</dt><dd>{when(node.added)}</dd></div>}
      </dl>

      {node.groups.length > 0 && (
        <p className="node-groups">
          {node.groups.map((group) => (
            <span key={group} className="badge badge-dim">{group}</span>
          ))}
        </p>
      )}

      <button className="btn btn-sm node-expand" onClick={toggle}
        disabled={!!state && !state.reachable}>
        {open ? 'Hide instances' : 'Show instances'}
      </button>

      {open && (
        detail === null ? <div className="loading-wrap"><span className="spinner" /></div>
          : detail.instances.length === 0 ? <p className="dim">No instances.</p>
            : (
              <ul className="node-instances">
                {detail.instances.map((instance) => (
                  <li key={instance.name}>
                    <span className={`badge ${instance.status === 'Running'
                      ? 'badge-ok' : 'badge-dim'}`}>{instance.status}</span>
                    <strong>{instance.name}</strong>
                    <span className="dim mono">{instance.ipv4[0] ?? ''}</span>
                    {instance.template && <span className="badge badge-dim">{instance.template}</span>}
                  </li>
                ))}
              </ul>
            )
      )}
    </article>
  )
}

/** Redeem a join code issued by the node being joined. */
function JoinDialog({ onCancel, onDone }: {
  onCancel: () => void
  onDone: (message: string, detail?: string) => void
}) {
  const [code, setCode] = useState('')
  const [description, setDescription] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  async function submit(event: React.FormEvent) {
    event.preventDefault()
    if (!code.trim() || busy) return
    setBusy(true)
    setError(null)
    try {
      const result = await api.joinNode(code.trim(), description.trim() || undefined)
      const detail = result.warning
        || (result.unreachable.length > 0
          ? `${result.unreachable.join(', ')} could not be told about this node yet.`
          : `Now a member alongside ${result.members.length - 1} other node(s).`)
      onDone(`Joined the cluster through ${result.node.name}`, detail)
    } catch (cause) {
      setError((cause as Error).message)
      setBusy(false)
    }
  }

  return (
    <Modal title="Join a cluster"
      subtitle="Paste a join code from any node in the cluster. This host works out its own name, address and certificate; nothing needs configuring first."
      onClose={busy ? () => {} : onCancel}
      footer={<>
        <button type="button" className="btn" onClick={onCancel} disabled={busy}>Cancel</button>
        <button type="submit" form="join-form" className="btn btn-primary"
          disabled={!code.trim() || busy}>
          {busy && <span className="spinner" />}Join
        </button>
      </>}>
      <form id="join-form" onSubmit={submit} style={{ display: 'grid', gap: 14 }}>
        <div className="field">
          <label htmlFor="join-code">Join code</label>
          <textarea id="join-code" className="input" rows={4} value={code}
            spellCheck={false} autoCapitalize="off"
            placeholder="lemondx-join.eyJ…"
            onChange={(event) => setCode(event.target.value)} />
          <span className="hint">
            From <code>lemondx cluster invite</code> on any node already in the cluster,
            or its <strong>Invite new node</strong> button. A code works once and expires.
            Joining one node makes this host a member of the whole cluster.
          </span>
        </div>
        <div className="field">
          <label htmlFor="join-description">Description</label>
          <input id="join-description" className="input" value={description} maxLength={200}
            placeholder="rack 2, spare capacity"
            onChange={(event) => setDescription(event.target.value)} />
        </div>
        {error && <span className="field-error" role="alert">{error}</span>}
      </form>
    </Modal>
  )
}

/** Issue a one-time code for another node to join this one with. */
function InviteDialog({ info, onClose }: { info: ClusterInfo | null; onClose: () => void }) {
  const [minutes, setMinutes] = useState(30)
  const [note, setNote] = useState('')
  const [code, setCode] = useState<JoinCode | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  // Nothing to satisfy up front any more -- issuing a code provisions this
  // node's identity and forms a cluster if there is none. Only an operator
  // switching enrolment off stands in the way.
  const blocked = info && !info.allow_enrollment
    ? 'Joining through this node is switched off (`lemondx configure cluster`).'
    : ''

  async function create(event: React.FormEvent) {
    event.preventDefault()
    setBusy(true)
    setError(null)
    try {
      setCode(await api.createInvite({ expires_minutes: minutes, note: note.trim() }))
    } catch (cause) {
      setError((cause as Error).message)
    } finally {
      setBusy(false)
    }
  }

  if (code) {
    return (
      <Modal title="Join code"
        subtitle="Copy it now — it is a credential and is never shown again. Press Join cluster on the joining host and paste it there; that host needs no setup of its own."
        onClose={onClose}
        footer={<button className="btn btn-primary" onClick={onClose}>Done</button>}>
        <div className="access-secret">
          <code className="mono" style={{ wordBreak: 'break-all' }}>{code.code}</code>
          <CopyButton text={code.code} label="join code" />
        </div>
        <dl className="node-facts" style={{ marginTop: 14 }}>
          <div><dt>Valid until</dt><dd>{when(code.expires)}</dd></div>
          <div><dt>Single use</dt><dd>redeeming it consumes it</dd></div>
          <div><dt>Joins a cluster of</dt><dd>{code.members} node(s)</dd></div>
          <div><dt>This node at</dt><dd className="mono">{code.node.url}</dd></div>
          <div>
            <dt>Certificate</dt>
            <dd className="mono" style={{ fontSize: 11 }}>{code.fingerprint_pretty}</dd>
          </div>
        </dl>
        {code.warning && (
          <div className="banner banner-warn" style={{ marginTop: 14 }}>
            <div className="banner-body"><p>{code.warning}</p></div>
          </div>
        )}
      </Modal>
    )
  }

  return (
    <Modal title="Invite a new node"
      subtitle="Makes a one-time code another lemondx redeems to federate with this one."
      onClose={busy ? () => {} : onClose}
      footer={<>
        <button type="button" className="btn" onClick={onClose} disabled={busy}>Cancel</button>
        <button type="submit" form="invite-form" className="btn btn-primary"
          disabled={busy || !!blocked}>
          {busy && <span className="spinner" />}Make code
        </button>
      </>}>
      {blocked ? (
        <div className="banner banner-warn"><div className="banner-body">
          <h3>This node cannot be joined yet</h3>
          <p>{blocked}</p>
        </div></div>
      ) : (
        <form id="invite-form" onSubmit={create} style={{ display: 'grid', gap: 14 }}>
          <div className="grid-2">
            <div className="field">
              <label htmlFor="invite-expiry">Valid for</label>
              <select id="invite-expiry" className="select" value={minutes}
                onChange={(event) => setMinutes(Number(event.target.value))}>
                <option value={5}>5 minutes</option>
                <option value={30}>30 minutes</option>
                <option value={120}>2 hours</option>
                <option value={1440}>24 hours</option>
              </select>
            </div>
            <div className="field">
              <label htmlFor="invite-note">Note</label>
              <input id="invite-note" className="input" value={note} maxLength={100}
                placeholder="who it is for" onChange={(event) => setNote(event.target.value)} />
            </div>
          </div>
          {info?.reachable_because && (
            <div className="banner banner-warn"><div className="banner-body">
              <h3>Peers may not be able to reach this node</h3>
              <p>{info.reachable_because}</p>
            </div></div>
          )}
          <p className="hint">
            The code admits whoever holds it to this cluster, with admin access on every
            node in it. Keep it to the short window you need, and send it over something
            private.
          </p>
          {error && <span className="field-error" role="alert">{error}</span>}
        </form>
      )}
    </Modal>
  )
}

function GroupDialog({ group, nodes, onCancel, onSaved }: {
  group: NodeGroup | null
  nodes: ClusterNode[]
  onCancel: () => void
  onSaved: (message: string) => void
}) {
  const [name, setName] = useState(group?.name ?? '')
  const [description, setDescription] = useState(group?.description ?? '')
  const [members, setMembers] = useState<string[]>(group?.members ?? [])
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  function toggle(node: string) {
    setMembers((current) => current.includes(node)
      ? current.filter((m) => m !== node) : [...current, node])
  }

  async function save(event: React.FormEvent) {
    event.preventDefault()
    if (!name.trim() || busy) return
    setBusy(true)
    setError(null)
    try {
      await api.saveNodeGroup(name.trim(), { members, description: description.trim() })
      onSaved(group ? `Updated group ${name.trim()}` : `Created group ${name.trim()}`)
    } catch (cause) {
      setError((cause as Error).message)
      setBusy(false)
    }
  }

  return (
    <Modal title={group ? `Edit ${group.name}` : 'New node group'}
      subtitle="A group is just a list of names; launching or syncing to it means all of them."
      onClose={busy ? () => {} : onCancel}
      footer={<>
        <button type="button" className="btn" onClick={onCancel} disabled={busy}>Cancel</button>
        <button type="submit" form="group-form" className="btn btn-primary"
          disabled={!name.trim() || busy}>
          {busy && <span className="spinner" />}Save
        </button>
      </>}>
      <form id="group-form" onSubmit={save} style={{ display: 'grid', gap: 14 }}>
        <div className="grid-2">
          <div className="field">
            <label htmlFor="group-name">Name</label>
            <input id="group-name" className="input" value={name} maxLength={64}
              disabled={!!group} placeholder="edge" spellCheck={false}
              onChange={(event) => setName(event.target.value)} />
            {group && <span className="hint">Renaming means making a new group.</span>}
          </div>
          <div className="field">
            <label htmlFor="group-description">Description</label>
            <input id="group-description" className="input" value={description} maxLength={200}
              onChange={(event) => setDescription(event.target.value)} />
          </div>
        </div>
        <div className="field">
          <label>Members</label>
          <div className="check-list">
            {nodes.map((node) => (
              <label key={node.name} className="check">
                <input type="checkbox" checked={members.includes(node.name)}
                  onChange={() => toggle(node.name)} />
                <span>{node.name}{node.self ? ' (this node)' : ''}</span>
              </label>
            ))}
          </div>
        </div>
        {error && <span className="field-error" role="alert">{error}</span>}
      </form>
    </Modal>
  )
}

/** Push this node's templates, modules or profiles to the others. */
function SyncDialog({ nodes, groups, onCancel, onNotify }: {
  nodes: ClusterNode[]
  groups: NodeGroup[]
  onCancel: () => void
  onNotify: Props['onNotify']
}) {
  const [kinds, setKinds] = useState<SyncKind[]>(['templates'])
  const [targets, setTargets] = useState<string[]>([])
  const [group, setGroup] = useState('')
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState<SyncResult | null>(null)
  const [error, setError] = useState<string | null>(null)

  const chosen = group ? [] : targets
  const canSync = kinds.length > 0 && (group !== '' || targets.length > 0) && !busy

  async function run(event: React.FormEvent) {
    event.preventDefault()
    if (!canSync) return
    setBusy(true)
    setError(null)
    try {
      const outcome = await api.syncToNodes({
        kinds,
        nodes: chosen.length ? chosen : undefined,
        groups: group ? [group] : undefined,
      })
      setResult(outcome)
      if (outcome.ok) {
        onNotify('success', `Synced ${outcome.items} item(s)`,
          `to ${outcome.nodes.join(', ')}`)
      } else {
        const failed = outcome.results.filter((r) => !r.ok)
        onNotify('error', `${failed.length} of ${outcome.results.length} did not sync`,
          failed.map((r) => `${r.node}/${r.name}: ${r.error}`).join('; '))
      }
    } catch (cause) {
      setError((cause as Error).message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal title="Sync to other nodes"
      subtitle="Copies this node's copy over theirs. There is no merge: what is here wins."
      onClose={busy ? () => {} : onCancel}
      footer={<>
        <button type="button" className="btn" onClick={onCancel} disabled={busy}>
          {result ? 'Close' : 'Cancel'}
        </button>
        <button type="submit" form="sync-form" className="btn btn-primary" disabled={!canSync}>
          {busy && <span className="spinner" />}Sync
        </button>
      </>}>
      <form id="sync-form" onSubmit={run} style={{ display: 'grid', gap: 14 }}>
        <div className="field">
          <label>What to copy</label>
          <div className="check-list">
            {SYNC_KINDS.map((kind) => (
              <label key={kind.id} className="check">
                <input type="checkbox" checked={kinds.includes(kind.id)}
                  onChange={() => setKinds((current) => current.includes(kind.id)
                    ? current.filter((k) => k !== kind.id) : [...current, kind.id])} />
                <span>{kind.label} <span className="dim">— {kind.hint}</span></span>
              </label>
            ))}
          </div>
        </div>

        <div className="field">
          <label htmlFor="sync-group">To a group</label>
          <select id="sync-group" className="select" value={group}
            onChange={(event) => setGroup(event.target.value)}>
            <option value="">Pick nodes instead</option>
            {groups.map((item) => (
              <option key={item.name} value={item.name}>{item.name}</option>
            ))}
          </select>
        </div>

        {!group && (
          <div className="field">
            <label>To nodes</label>
            <div className="check-list">
              {nodes.map((node) => (
                <label key={node.name} className="check">
                  <input type="checkbox" checked={targets.includes(node.name)}
                    onChange={() => setTargets((current) => current.includes(node.name)
                      ? current.filter((n) => n !== node.name) : [...current, node.name])} />
                  <span>{node.name}</span>
                </label>
              ))}
            </div>
          </div>
        )}

        {error && <span className="field-error" role="alert">{error}</span>}

        {result && (
          <ul className="sync-results">
            {result.results.map((row) => (
              <li key={`${row.node}/${row.kind}/${row.name}`}
                className={row.ok ? 'sync-ok' : 'sync-failed'}>
                <span className="badge badge-dim">{row.node}</span>
                <span className="dim">{row.kind}</span>
                <strong>{row.name}</strong>
                {!row.ok && <span className="node-error">{row.error}</span>}
              </li>
            ))}
          </ul>
        )}
      </form>
    </Modal>
  )
}
