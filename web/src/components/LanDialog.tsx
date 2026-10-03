import { useEffect, useState } from 'react'
import type { FormEvent } from 'react'
import { api, calls } from '../lib/api'
import type { LanInterface, LanInterfaces, LanMode, LanStep } from '../lib/types'
import { CopyButton } from './CopyButton'
import { Modal } from './Modal'
import { BackButton } from './NetworkWizard'

export interface LanChoice {
  nic: string
  mode: LanMode
  name: string
  /** On every member, each on the NIC its own default route leaves by. */
  everywhere: boolean
}

interface Props {
  onCancel: () => void
  /** Back to the "New network" wizard's first step. */
  onBack?: () => void
  /** Resolves when the change is made; rejects with the server's reason. */
  onSubmit: (choice: LanChoice) => Promise<void>
}

/** Mirrors service.VALID_NETWORK_NAME: the kernel's 15-character limit. */
const NAME_RULE = /^[a-zA-Z][a-zA-Z0-9-]{0,14}$/

// macvlan first, and chosen whenever a NIC allows it: it changes nothing on
// the host, so it is the one to reach for unless the host must talk to them.
const MODES: { id: LanMode; label: string; about: string }[] = [
  { id: 'macvlan', label: 'macvlan',
    about: 'Each instance gets its own MAC beside the host’s, on the same NIC. Nothing '
      + 'on the host changes — but the host itself cannot reach these instances over '
      + 'that NIC; everything else on the LAN can.' },
  { id: 'bridge', label: 'Bridge a spare NIC',
    about: 'A daemon bridge with the NIC as its port. Nothing on the host moves, since '
      + 'the NIC carries no address of its own.' },
  { id: 'convert', label: 'Bridge the host’s own NIC',
    about: 'A real bridge: the NIC becomes its port and the host’s address moves onto '
      + 'the bridge, so the host and its instances reach each other like any two '
      + 'machines on the LAN. Done through NetworkManager, which keeps it across '
      + 'reboots.' },
]

const EVERYWHERE_ONLY = 'Only macvlan can be made on every node at once: converting moves '
  + 'each host’s own connection, and is done node by node.'

function defaultName(nic: string, mode: LanMode, everywhere = false) {
  if (everywhere) return 'lan'
  return mode === 'convert' ? 'br0' : `lan-${nic}`.slice(0, 15)
}

/**
 * Put instances on the network a host NIC is on, addressed by that network's
 * own DHCP (usually the router) rather than the daemon's: no NAT, no daemon
 * DHCP. What a NIC allows depends on it, so the NIC is picked first and only
 * what works for it is offered.
 */
export function LanDialog({ onCancel, onBack, onSubmit }: Props) {
  const [found, setFound] = useState<LanInterfaces | null>(null)
  const [members, setMembers] = useState(1)
  const [everywhere, setEverywhere] = useState(false)
  const [loadError, setLoadError] = useState('')
  const [nic, setNic] = useState('')
  const [mode, setMode] = useState<LanMode | null>(null)
  const [name, setName] = useState('')
  const [confirmText, setConfirmText] = useState('')
  const [plan, setPlan] = useState<{ key: string; steps: LanStep[] } | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    const controller = new AbortController()
    api.lanInterfaces(controller.signal).then((next) => {
      setFound(next)
      // The first NIC anything can be done with, so the dialog opens ready.
      const first = next.interfaces.find((n) => Object.values(n.modes).some((m) => m.available || m.manual))
      if (first) pick(first)
    }).catch((cause) => {
      if ((cause as Error).name !== 'AbortError') setLoadError((cause as Error).message)
    })
    // Only how many: the tick box is offered where there is more than one.
    api.nodes(controller.signal, false).then((list) => setMembers(list.length)).catch(() => {})
    return () => controller.abort()
  }, [])

  function pick(target: LanInterface, nextMode?: LanMode) {
    const chosen = nextMode ?? MODES.map((m) => m.id).find(
      (id) => target.modes[id].available)
      ?? MODES.map((m) => m.id).find((id) => target.modes[id].manual) ?? null
    setNic(target.name)
    setMode(chosen)
    setName(chosen ? defaultName(target.name, chosen) : '')
    setConfirmText('')
    setError('')
  }

  const selected = found?.interfaces.find((n) => n.name === nic) ?? null
  const converting = mode === 'convert'

  function spread(on: boolean) {
    setEverywhere(on)
    setError('')
    if (on) {
      setMode('macvlan')
      setName(defaultName(nic, 'macvlan', true))
    } else if (selected) {
      pick(selected)
    }
  }
  const manual = Boolean(selected && converting && selected.modes.convert.manual)
  const planKey = `${nic}/${name}`

  // The commands are fetched for the bridge name as typed, debounced, so what
  // is read -- or pasted, without the helper -- is exactly what would run.
  useEffect(() => {
    if (!converting || !nic || !NAME_RULE.test(name)) return
    const controller = new AbortController()
    const timer = window.setTimeout(() => {
      api.lanPlan(nic, name, controller.signal)
        .then((steps) => setPlan({ key: `${nic}/${name}`, steps }))
        .catch(() => {})
    }, 300)
    return () => { controller.abort(); window.clearTimeout(timer) }
  }, [converting, nic, name])
  const steps = plan && plan.key === planKey ? plan.steps : null

  const nameValid = NAME_RULE.test(name)
  const canSubmit = everywhere
    ? nameValid && !busy
    : Boolean(selected && mode && selected.modes[mode].available && nameValid
      && !busy && (!converting || confirmText.trim() === nic))

  async function submit(event: FormEvent) {
    event.preventDefault()
    if (!canSubmit || !mode) return
    setBusy(true)
    setError('')
    try {
      await onSubmit({ nic, mode: everywhere ? 'macvlan' : mode, name, everywhere })
    } catch (cause) {
      setError((cause as Error).message)
      setBusy(false)
    }
  }

  const apiCall = everywhere ? calls.createLanEverywhere(name)
    : !mode || !nic ? null
      : converting ? calls.convertNic(nic, name)
        : calls.createLanNetwork({ nic, mode, name })

  return (
    <Modal
      title="Network on your LAN"
      subtitle="Instances get their address from that network's own DHCP — usually your router — with no NAT and no daemon DHCP in between."
      onClose={busy ? () => {} : onCancel}
      api={apiCall}
      footer={
        <>
          {onBack && <BackButton onClick={onBack} disabled={busy} />}
          <button type="button" className="btn" onClick={onCancel} disabled={busy}>
            {manual ? 'Close' : 'Cancel'}
          </button>
          {!manual && (
            <button className={`btn ${converting ? 'btn-danger' : 'btn-primary'}`}
              form="lan-form" disabled={!canSubmit}>
              {busy && <span className="spinner" />}
              {busy && converting ? 'Converting…'
                : converting ? `Convert ${nic || 'NIC'}`
                  : everywhere ? `Create on ${members} nodes` : 'Create network'}
            </button>
          )}
        </>
      }
    >
      <form id="lan-form" onSubmit={submit} style={{ display: 'contents' }}>
        {loadError && <div className="field-error">{loadError}</div>}
        {!found && !loadError && (
          <div className="loading-wrap"><span className="spinner" /> Looking at this host’s NICs…</div>
        )}
        {found && found.interfaces.length === 0 && (
          <p className="hint">The daemon reports no network cards on this host.</p>
        )}

        {found && members > 1 && (
          <label className="checkbox">
            <input type="checkbox" checked={everywhere} disabled={busy}
              onChange={(e) => spread(e.target.checked)} />
            Apply to the default interface on all nodes
          </label>
        )}

        {found && everywhere && (
          <p className="hint" style={{ marginTop: -6 }}>
            Each of the {members} nodes uses the NIC its own default route leaves by
            {found.default_nic ? <> — here, <span className="mono">{found.default_nic}</span></> : null},
            since NIC names differ between machines. The network gets the same name on every
            node, so a template can use it wherever it launches. A node that already has a
            macvlan by that name on its default NIC is left as it is.
          </p>
        )}

        {found && !everywhere && found.interfaces.length > 0 && (
          <div className="field">
            <label>Network card</label>
            <div className="check-list">
              {found.interfaces.map((n) => (
                <label key={n.name} className="check" style={{ alignItems: 'flex-start' }}>
                  <input type="radio" name="lan-nic" checked={nic === n.name} disabled={busy}
                    onChange={() => pick(n)} />
                  <span>
                    <strong className="mono">{n.name}</strong>
                    <span className="faint">
                      {' '}· {n.wireless ? 'Wi-Fi' : 'wired'}
                      {n.up ? '' : ' · down'}
                      {n.default_route ? ' · the host’s way out' : ''}
                      {n.master ? ` · port of ${n.master}` : ''}
                    </span>
                    <div className="faint mono" style={{ fontSize: 11.5 }}>
                      {n.addresses.join(', ') || 'no address'}
                    </div>
                  </span>
                </label>
              ))}
            </div>
          </div>
        )}

        {(selected || everywhere) && (
          <div className="field">
            <label>How</label>
            <div className="check-list" style={{ maxHeight: 'none' }}>
              {MODES.map((m) => {
                const state = everywhere
                  ? { available: m.id === 'macvlan', manual: false,
                      reason: m.id === 'macvlan' ? '' : EVERYWHERE_ONLY }
                  : selected ? selected.modes[m.id]
                    : { available: false, manual: false, reason: '' }
                const offered = state.available || state.manual
                return (
                  <label key={m.id} className="check"
                    style={{ alignItems: 'flex-start', opacity: offered ? 1 : 0.55 }}>
                    <input type="radio" name="lan-mode" checked={mode === m.id}
                      disabled={busy || !offered}
                      onChange={() => selected && pick(selected, m.id)} />
                    <span>
                      <strong>{m.label}</strong>
                      <div className="faint" style={{ fontSize: 12 }}>
                        {offered ? m.about : state.reason}
                      </div>
                    </span>
                  </label>
                )
              })}
            </div>
          </div>
        )}

        {(selected || everywhere) && mode && (
          <div className="field">
            <label htmlFor="lan-name">{converting ? 'Bridge name' : 'Network name'}</label>
            <input id="lan-name" className="input mono" value={name} maxLength={15}
              disabled={busy} autoComplete="off" aria-invalid={!nameValid}
              onChange={(e) => setName(e.target.value)} />
            {!nameValid && (
              <span className="field-error">
                Letters, digits and dashes, starting with a letter; at most 15.
              </span>
            )}
          </div>
        )}

        {converting && selected && (
          <>
            <div className="banner banner-error" style={{ margin: 0, padding: '10px 12px' }}>
              <div className="banner-body">
                <p style={{ margin: 0 }}>
                  This moves the host’s own connection
                  {selected.addresses.length ? <> ({selected.addresses.join(', ')})</> : null}{' '}
                  from <span className="mono">{nic}</span> onto{' '}
                  <span className="mono">{name || 'the bridge'}</span>. The network drops for a
                  few seconds — this page may lose the server briefly. The bridge takes the
                  NIC’s MAC, addresses, gateway and DNS, so the router should hand back the
                  same address. If the bridge does not come up with an address
                  {selected.default_route ? ' and the host’s way out' : ''}, it is put back
                  as it was. Undo it later with <strong>Revert</strong> on the bridge.
                </p>
              </div>
            </div>
            {selected.modes.convert.manual && (
              <p className="hint">{selected.modes.convert.reason}</p>
            )}
            <div className="field">
              <label>
                What it runs
                {steps && (
                  <CopyButton text={steps.map((s) => s.command).join('\n')} label="Copy commands" />
                )}
              </label>
              {steps ? (
                <pre className="mono" style={{ fontSize: 11.5, whiteSpace: 'pre-wrap', margin: 0 }}>
                  {steps.map((s) => `# ${s.why}\n${s.command}`).join('\n')}
                </pre>
              ) : <span className="hint">{nameValid ? 'Working it out…' : '—'}</span>}
            </div>
            {!manual && (
              <div className="field">
                <label htmlFor="lan-confirm">
                  Type <span className="mono">{nic}</span> to confirm
                </label>
                <input id="lan-confirm" className="input mono" value={confirmText}
                  disabled={busy} autoComplete="off"
                  onChange={(e) => setConfirmText(e.target.value)} />
              </div>
            )}
          </>
        )}

        {found?.docker && mode && mode !== 'macvlan' && (
          <p className="hint">
            Docker is installed here, and its firewall drops bridged traffic. lemondx
            {found.helper ? ' adds an exception for the bridge'
              : ' cannot run its helper as root, so it will show the exception to add yourself'}.
          </p>
        )}

        {error && (
          <div className="banner banner-error" style={{ margin: 0, padding: '10px 12px' }}>
            <div className="banner-body"><p style={{ margin: 0, color: 'var(--danger)' }}>{error}</p></div>
          </div>
        )}
      </form>
    </Modal>
  )
}
