import { useEffect, useState } from 'react'
import { api, calls } from '../lib/api'
import type { ClusterNode, ImageJob, PublishRequest } from '../lib/types'
import { Modal } from './Modal'

interface Props {
  instance: string
  snapshot: string
  /** The node the snapshot is on, when that is not this one. */
  node?: string
  onCancel: () => void
  onStarted: (job: ImageJob) => void
}

/** Mirrors service.VALID_ALIAS. */
const ALIAS_RULE = /^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$/

/**
 * Make a snapshot into an image, and copy it to the nodes picked. The image is
 * always kept where the snapshot is, since that is where it is made; each
 * other node gets a full copy, which for a large instance is gigabytes -- so
 * the choice is spelled out here rather than made by a launch.
 */
export function PublishDialog({ instance, snapshot, node, onCancel, onStarted }: Props) {
  const [alias, setAlias] = useState(`${instance}-${snapshot}`.toLowerCase()
    .replace(/[^a-z0-9._-]+/g, '-').slice(0, 64))
  const [description, setDescription] = useState('')
  const [members, setMembers] = useState<ClusterNode[] | null>(null)
  const [chosen, setChosen] = useState<string[]>([])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    const controller = new AbortController()
    // Unprobed: only the names are wanted, and probing every peer is slow.
    api.nodes(controller.signal, false).then(setMembers).catch(() => setMembers([]))
    return () => controller.abort()
  }, [])

  const owner = node ?? members?.find((m) => m.self)?.name ?? ''
  const others = (members ?? []).filter((m) => m.name !== owner)
  const aliasValid = ALIAS_RULE.test(alias.trim())
  const body: PublishRequest = {
    alias: alias.trim(), nodes: chosen, description: description.trim() || undefined,
  }

  async function submit(event: React.FormEvent) {
    event.preventDefault()
    if (!aliasValid || busy) return
    setBusy(true)
    setError(null)
    try {
      onStarted(await api.publishSnapshot(instance, snapshot, body, node))
    } catch (cause) {
      setError((cause as Error).message)
      setBusy(false)
    }
  }

  return (
    <Modal
      title={`Make an image of “${snapshot}”`}
      subtitle={`From ${instance}${owner ? ` on ${owner}` : ''}. Templates launch it as local:${alias.trim() || '<alias>'} on every node that has it.`}
      onClose={busy ? () => {} : onCancel}
      api={calls.publishSnapshot(instance, snapshot, body, node)}
      footer={
        <>
          <button type="button" className="btn" onClick={onCancel} disabled={busy}>Cancel</button>
          <button type="submit" form="publish-form" className="btn btn-primary"
            disabled={!aliasValid || busy}>
            {busy && <span className="spinner" />}
            {chosen.length ? `Make image and copy to ${chosen.length}` : 'Make image'}
          </button>
        </>
      }
    >
      <form id="publish-form" onSubmit={submit} style={{ display: 'contents' }}>
        <div className="field">
          <label htmlFor="p-alias">Image name</label>
          <input id="p-alias" className="input mono" value={alias} disabled={busy}
            onChange={(e) => setAlias(e.target.value)} maxLength={64} autoComplete="off"
            aria-invalid={!aliasValid} autoFocus />
          <span className="hint">
            {aliasValid ? (
              <>Must be free on {owner || 'this node'}. On the nodes it is copied to, an image
                already called this is replaced by the copy.</>
            ) : (
              <span style={{ color: 'var(--danger)' }}>
                Letters, digits, dots, dashes and underscores, starting with a letter or digit.
              </span>
            )}
          </span>
        </div>
        <div className="field">
          <label htmlFor="p-desc">Description</label>
          <input id="p-desc" className="input" value={description} disabled={busy}
            onChange={(e) => setDescription(e.target.value)} maxLength={200}
            placeholder={`${instance}/${snapshot}, published by lemondx`} autoComplete="off" />
        </div>
        {others.length > 0 && (
          <div className="field">
            <label>Copy to</label>
            <div className="check-list">
              {others.map((member) => (
                <label key={member.name} className="check">
                  <input type="checkbox" disabled={busy} checked={chosen.includes(member.name)}
                    onChange={() => setChosen(chosen.includes(member.name)
                      ? chosen.filter((n) => n !== member.name) : [...chosen, member.name])} />
                  <span>{member.name}{member.self ? ' (this node)' : ''}</span>
                </label>
              ))}
            </div>
            <span className="hint">
              Each node gets the whole image, sent from {owner || 'this node'} over the
              cluster&apos;s own connection and checked on arrival. It carries on if you
              close this page; progress shows on the snapshot.
            </span>
          </div>
        )}
        {error && (
          <div className="banner banner-error" style={{ margin: 0, padding: '10px 12px' }}>
            <div className="banner-body">
              <p style={{ margin: 0, color: 'var(--danger)' }}>{error}</p>
            </div>
          </div>
        )}
      </form>
    </Modal>
  )
}
