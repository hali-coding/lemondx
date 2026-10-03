import { useEffect, useState } from 'react'
import type { ReactNode } from 'react'
import { api } from '../lib/api'
import { Modal } from './Modal'

/** The kinds of network the wizard can make; each continues in its own form. */
export type NetworkKind = 'bridge' | 'fabric' | 'lan'

interface Props {
  onCancel: () => void
  onPick: (kind: NetworkKind) => void
}

interface Choice {
  kind: NetworkKind
  title: string
  /** The question it answers, in the person's terms rather than the daemon's. */
  reach: string
  about: ReactNode
  /** What the next step will ask. */
  next: string
}

/**
 * The first step of "New network": what should instances on it reach? Every
 * kind of network lemondx makes answers one version of that question, so the
 * wizard starts there rather than with bridges, fabrics and NICs, and hands on
 * to the form for the answer picked.
 */
export function NetworkWizard({ onCancel, onPick }: Props) {
  const [members, setMembers] = useState<number | null>(null)
  const [picked, setPicked] = useState<NetworkKind>('bridge')

  useEffect(() => {
    const controller = new AbortController()
    // Unprobed: only how many there are, for what a fabric would span.
    api.nodes(controller.signal, false).then((list) => setMembers(list.length))
      .catch(() => setMembers(null))
    return () => controller.abort()
  }, [])

  const choices: Choice[] = [
    {
      kind: 'bridge',
      title: 'Private to this node',
      reach: 'Each other on this host, and the internet',
      about: <>
        lemondx hands out the addresses and NATs the instances behind the host, like the
        default <span className="mono">lxdbr0</span>. Nothing outside this host can reach
        them. The usual choice when unsure.
      </>,
      next: 'Next: its name and address range.',
    },
    {
      kind: 'fabric',
      title: 'Shared across the cluster (fabric)',
      reach: 'Each other on every node, by their own addresses',
      about: <>
        One network on every node, each node with its own /24, routed between them — a
        stack’s tiers can talk directly whichever host they landed on, and every node
        reaches them too.
        {members === 1 && <> This node is not in a cluster yet, so it spans this host until
          others join.</>}
        {members !== null && members > 1 && <> It will span all {members} nodes.</>}
      </>,
      next: 'Next: its name and prefix, checked against what every node already uses.',
    },
    {
      kind: 'lan',
      title: 'On your LAN',
      reach: 'Everything on your network, like any other machine',
      about: <>
        Instances get their address from your router, with no NAT and no DHCP from
        lemondx, and are reachable from anything on that network. They still reach
        every fabric.
      </>,
      next: 'Next: which network card, and how — macvlan (nothing on the host changes), a spare NIC as a bridge, or this host’s own NIC as a bridge.',
    },
  ]

  return (
    <Modal
      title="New network"
      subtitle="What should instances on it be able to reach?"
      onClose={onCancel}
      footer={
        <>
          <button type="button" className="btn" onClick={onCancel}>Cancel</button>
          <button type="button" className="btn btn-primary" onClick={() => onPick(picked)}>
            Continue
          </button>
        </>
      }
    >
      <div className="check-list" role="radiogroup" aria-label="Kind of network"
        style={{ maxHeight: 'none', gap: 10 }}>
        {choices.map((choice) => (
          <label key={choice.kind} className="check" style={{ alignItems: 'flex-start' }}>
            <input type="radio" name="network-kind" checked={picked === choice.kind}
              onChange={() => setPicked(choice.kind)}
              onDoubleClick={() => onPick(choice.kind)} />
            <span>
              <strong>{choice.title}</strong>
              <span className="faint"> — {choice.reach}</span>
              <div className="faint" style={{ fontSize: 12, marginTop: 2 }}>{choice.about}</div>
              {picked === choice.kind && (
                <div className="hint" style={{ fontSize: 12, marginTop: 4 }}>{choice.next}</div>
              )}
            </span>
          </label>
        ))}
      </div>
    </Modal>
  )
}

/** The wizard's way back to its first step, at the start of a form's footer. */
export function BackButton({ onClick, disabled }: { onClick: () => void; disabled?: boolean }) {
  return (
    <button type="button" className="btn btn-ghost" style={{ marginRight: 'auto' }}
      onClick={onClick} disabled={disabled}>
      ← Back
    </button>
  )
}
