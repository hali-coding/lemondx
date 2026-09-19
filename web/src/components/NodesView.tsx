import { useCallback, useEffect, useState } from 'react'
import { useCanWrite } from '../hooks/useAuth'
import { api } from '../lib/api'
import type {
  AutoGroupResult, ClusterInfo, ClusterNode, ClusterNodeDetail, EvictResult, JoinCode,
  LeaveResult, NodeGroup, NodeTold, SyncKind, SyncResult,
} from '../lib/types'
import { ConfirmDialog } from './ConfirmDialog'
import { CopyButton } from './CopyButton'
import {
  EjectIcon, KeyIcon, LogoutIcon, PlusIcon, RefreshIcon, ScalesIcon, ServerIcon, TrashIcon,
  UploadIcon,
} from './Icons'
import { Modal } from './Modal'

interface Props {
  onNotify: (kind: 'success' | 'error' | 'info', title: string, detail?: string) => void
  /**
   * Joining, evicting and leaving change who the Containers and Templates tabs
   * can be scoped to, and that list is read once rather than polled — so the
   * scope picker would go on offering a node this tab has just removed, or
   * survive a `leave` that emptied the cluster, until a reload.
   */
  onMembershipChanged: () => void
}

// Reachability comes from the server's own probe, which it caches for a few
// seconds; polling faster than that would only re-read the same answer.
const POLL_INTERVAL = 10000

const SYNC_KINDS: { id: SyncKind; label: string; hint: string; warn?: string }[] = [
  { id: 'templates', label: 'Templates', hint: 'with the uploaded modules they use' },
  { id: 'modules', label: 'Modules', hint: 'uploaded ones; built-ins are everywhere already' },
  { id: 'profiles', label: 'Bootstrap profiles', hint: 'named module selections' },
  { id: 'groups', label: 'Node groups', hint: 'including the sized large and small' },
  {
    id: 'users', label: 'Users', hint: 'local accounts, so the same logins work there',
    warn: 'Each account is copied with its password hash and role, replacing any '
      + 'account of the same name on the target. Accounts that exist only there are '
      + 'left alone — sync never deletes.',
  },
]

function when(epoch: number | null | undefined) {
  return epoch ? new Date(epoch * 1000).toLocaleString() : '—'
}

function short(fingerprint: string) {
  return fingerprint ? `${fingerprint.slice(0, 8)}…${fingerprint.slice(-8)}` : '—'
}

const SIZE_GROUP_NAMES = 'large and small'

/** What the sizing decided, short enough for a toast. */
function sizeDetail(result: AutoGroupResult) {
  const parts = result.uniform
    ? ['Every node is the same size, so each is in both groups.']
    : result.groups.map((group) => `${group.name}: ${group.members.join(', ') || 'none'}.`)
  if (result.skipped.length > 0) {
    parts.push(`${result.skipped.join(', ')} could not be measured and is in neither.`)
  }
  return parts.join(' ')
}

function stragglers(told: NodeTold[]) {
  return told.filter((row) => !row.ok).map((row) => row.node)
}

/**
 * What is left to do by hand, if anything. Both removals are best-effort
 * against hosts that may be off, so the one thing the toast must carry is
 * which node did not get the message — that is the only part someone has to
 * act on, and nothing else will bring it up again.
 */
function evictDetail(result: EvictResult) {
  const parts: string[] = []
  parts.push(result.stood_down
    ? `${result.removed} stood down and gave up the cluster credential.`
    : `${result.removed} could not be told (${result.stand_down_error ?? 'unreachable'}).`)
  if (result.rotated) {
    parts.push(result.rotated.stranded.length === 0
      ? 'The cluster credential was replaced on every member.'
      : `The credential was replaced, but ${result.rotated.stranded.join(', ')} missed it and must rejoin.`)
  } else if (result.still_holds_credential) {
    parts.push('It still holds the cluster credential — run `lemondx cluster rotate` to cut it off.')
  }
  const behind = stragglers(result.told)
  if (behind.length > 0) parts.push(`${behind.join(', ')} still lists it.`)
  return parts.join(' ')
}

function leaveDetail(result: LeaveResult) {
  const gone = result.left.length === 0 ? 'Nothing was federated.'
    : `Forgot ${result.left.join(', ')}.`
  return result.stale.length === 0 ? gone
    : `${gone} ${result.stale.join(', ')} could not be told and still lists this node.`
}

/** Nodes this lemondx federates with, the groups they form, and what spans them. */
export function NodesView({ onNotify, onMembershipChanged }: Props) {
  const canWrite = useCanWrite()
  const [info, setInfo] = useState<ClusterInfo | null>(null)
  const [nodes, setNodes] = useState<ClusterNode[] | null>(null)
  const [groups, setGroups] = useState<NodeGroup[]>([])
  const [error, setError] = useState<string | null>(null)
  const [dialog, setDialog] = useState<'join' | 'invite' | 'sync' | null>(null)
  const [editingGroup, setEditingGroup] = useState<NodeGroup | 'new' | null>(null)
  const [pendingEvict, setPendingEvict] = useState<ClusterNode | null>(null)
  const [pendingLeave, setPendingLeave] = useState(false)
  const [pendingSize, setPendingSize] = useState(false)
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

  async function evictNode() {
    if (!pendingEvict) return
    const name = pendingEvict.name
    setBusy(true)
    try {
      const result = await api.evictNode(name)
      onNotify(result.stood_down && stragglers(result.told).length === 0 ? 'success' : 'info',
        `Evicted ${name}`, evictDetail(result))
      onMembershipChanged()
      await load()
    } catch (cause) {
      onNotify('error', `Could not evict ${name}`, (cause as Error).message)
    } finally {
      setBusy(false)
      setPendingEvict(null)
    }
  }

  async function leaveCluster() {
    setBusy(true)
    try {
      const result = await api.leaveCluster()
      onNotify(result.stale.length === 0 ? 'success' : 'info', 'Left the cluster',
        leaveDetail(result))
      onMembershipChanged()
      await load()
    } catch (cause) {
      onNotify('error', 'Could not leave the cluster', (cause as Error).message)
    } finally {
      setBusy(false)
      setPendingLeave(false)
    }
  }

  async function sizeGroups() {
    setBusy(true)
    try {
      const result = await api.autoGroups()
      onNotify(result.skipped.length === 0 ? 'success' : 'info',
        'Sized the nodes', sizeDetail(result))
      await load()
    } catch (cause) {
      onNotify('error', 'Could not size the nodes', (cause as Error).message)
    } finally {
      setBusy(false)
      setPendingSize(false)
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

      {info && (
        <ThisNode info={info} peers={peers.length} canWrite={canWrite}
          onLeave={() => setPendingLeave(true)} />
      )}

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
                onEvict={() => setPendingEvict(node)} onNotify={onNotify} />
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
          <div className="row-actions access-head-end">
            <button className="btn btn-sm" disabled={!canWrite}
              onClick={() => setPendingSize(true)}
              title="Rebuild large and small from each node's CPU and memory">
              <ScalesIcon /> Size nodes
            </button>
            <button className="btn btn-sm btn-primary" disabled={!canWrite}
              onClick={() => setEditingGroup('new')}>
              <PlusIcon /> New group
            </button>
          </div>
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
                  <td>
                    <strong>{group.name}</strong>
                    {group.managed && (
                      <span className="badge badge-dim" style={{ marginLeft: 6 }}
                        title="Sized by lemondx from each node's CPU and memory">
                        auto
                      </span>
                    )}
                  </td>
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
                    {/* No controls at all on a managed row rather than disabled
                        ones: the server refuses these two names outright, so an
                        Edit that could only ever fail is worse than none. */}
                    {!group.managed && (
                      <div className="row-actions">
                        <button className="btn btn-sm" disabled={!canWrite}
                          onClick={() => setEditingGroup(group)}>Edit</button>
                        <button className="btn btn-sm btn-icon btn-danger" disabled={!canWrite}
                          aria-label={`Delete group ${group.name}`} title="Delete"
                          onClick={() => deleteGroup(group.name)}>
                          <TrashIcon />
                        </button>
                      </div>
                    )}
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
            onMembershipChanged()
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

      {pendingEvict && (
        <ConfirmDialog
          title={`Evict ${pendingEvict.name} from the cluster?`}
          message={`${pendingEvict.name} is told to stand down — it gives up the cluster credential, its member list and its node groups — and every remaining member is told to forget it. Its instances, templates and modules are untouched, and it goes on working on its own. If it cannot be reached, the credential is replaced here instead, which also cuts off any member that is switched off right now.`}
          confirmLabel="Evict"
          confirmText={pendingEvict.name}
          danger
          busy={busy}
          onConfirm={evictNode}
          onCancel={() => setPendingEvict(null)}
        />
      )}

      {pendingSize && (
        <ConfirmDialog
          title="Size the nodes?"
          message={`Every node is asked what CPU and memory it has, and the ${SIZE_GROUP_NAMES} groups are rebuilt from the answers: a node at or above the cluster average goes in large, at or below it in small, so a cluster of identical hosts puts every node in both. Whatever those two groups hold now is replaced, and the new lists are pushed to every member. A node that cannot be reached is left out of both rather than guessed at.`}
          confirmLabel="Size nodes"
          busy={busy}
          onConfirm={sizeGroups}
          onCancel={() => setPendingSize(false)}
        />
      )}

      {pendingLeave && info && (
        <ConfirmDialog
          title={`Take ${info.node.name} out of the cluster?`}
          message={`Every other member is told to forget this node, and this node gives up the cluster credential, its record of the ${peers.length} other node${peers.length === 1 ? '' : 's'} and its node groups. Instances, templates and modules stay exactly as they are; only federation stops. Rejoining needs a fresh join code.`}
          confirmLabel="Leave cluster"
          confirmText={info.node.name}
          danger
          busy={busy}
          onConfirm={leaveCluster}
          onCancel={() => setPendingLeave(false)}
        />
      )}
    </>
  )
}

/** What this node looks like to the others, and whether anything can join it. */
function ThisNode({ info, peers, canWrite, onLeave }: {
  info: ClusterInfo
  peers: number
  canWrite: boolean
  onLeave: () => void
}) {
  return (
    <section className="card node-self">
      <div className="node-self-main">
        <h3>
          <ServerIcon size={16} /> {info.node.name}
          <span className="badge badge-dim">this node</span>
          {/* Leaving lives here rather than on the node's card in the grid: it
              is the one action that acts on this host, and the grid's buttons
              all act on somebody else. */}
          {info.in_cluster && (
            <button className="btn btn-sm btn-danger node-self-leave" disabled={!canWrite}
              onClick={onLeave}
              title="Take this node out of the cluster and have every member forget it">
              <LogoutIcon /> Leave cluster
            </button>
          )}
        </h3>
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

function NodeCard({ node, canWrite, onEvict, onNotify }: {
  node: ClusterNode
  canWrite: boolean
  onEvict: () => void
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

      {/* Both actions spelled out, side by side. Eviction used to be an X in
          the corner of the card, which is the shape of a control for undoing a
          mistake -- and this is the one control here that cannot be undone
          without a fresh join code on the other host. */}
      <div className="row-actions node-card-actions">
        <button className="btn btn-sm node-expand" onClick={toggle}
          disabled={!!state && !state.reachable}>
          {open ? 'Hide instances' : 'Show instances'}
        </button>
        {!node.self && (
          <button className="btn btn-sm btn-danger" disabled={!canWrite} onClick={onEvict}
            title={`Put ${node.name} out of the cluster`}>
            <EjectIcon /> Evict
          </button>
        )}
      </div>

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
            {group ? <span className="hint">Renaming means making a new group.</span>
              : <span className="hint">
                  <code>large</code> and <code>small</code> are kept by <strong>Size
                  nodes</strong> and cannot be used here.
                </span>}
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
          {/* Only once it is actually chosen: a standing warning about an
              option nobody ticked is noise, and stops being read. */}
          {SYNC_KINDS.filter((kind) => kind.warn && kinds.includes(kind.id)).map((kind) => (
            <span key={kind.id} className="hint">{kind.label}: {kind.warn}</span>
          ))}
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
