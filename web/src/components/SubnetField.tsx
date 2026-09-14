import { formatAddress, hostCount, isPrivate, subnetStatus, suggestBlock } from '../lib/cidr'
import type { Family } from '../lib/cidr'
import type { SubnetInUse } from '../lib/types'

interface Props {
  id: string
  family: Family
  /** `auto`, `none`, or a CIDR block as typed. */
  value: string
  onChange: (value: string) => void
  /** Subnets already on the host, excluding the network being edited. */
  used: SubnetInUse[] | null
  allowNone?: boolean
  disabled?: boolean
  /** Enter in the block field; without it Enter submits the surrounding form. */
  onEnter?: () => void
}

function count(value: bigint, spareBits: number) {
  return value < 1_000_000n ? value.toLocaleString() : `about 2^${spareBits}`
}

export function SubnetField({
  id, family, value, onChange, used, allowNone = true, disabled, onEnter,
}: Props) {
  const mode = value === 'auto' || value === 'none' ? value : 'custom'
  const { error, block } = subnetStatus(value, family, used)
  const bits = family === 4 ? 32 : 128
  const others = (used ?? []).filter((entry) => entry.family === family)

  return (
    <div className="subnet-field">
      <div className="grid-2">
        <select className="select" value={mode} disabled={disabled}
          aria-label={`IPv${family} addressing`}
          onChange={(event) => {
            const next = event.target.value
            onChange(next === 'custom' ? suggestBlock(family, used ?? []) : next)
          }}>
          <option value="auto">Pick a free subnet for me</option>
          <option value="custom">Choose the CIDR block…</option>
          {allowNone && <option value="none">Disabled</option>}
        </select>
        {mode === 'custom' ? (
          <input id={id} className="input mono" value={value} disabled={disabled} required
            placeholder={family === 4 ? '10.20.0.0/24' : 'fd42:20::/64'}
            aria-label={`IPv${family} CIDR block`} aria-invalid={!!error} autoComplete="off"
            spellCheck={false} onChange={(event) => onChange(event.target.value)}
            onKeyDown={onEnter && ((event) => {
              if (event.key === 'Enter') { event.preventDefault(); onEnter() }
            })} />
        ) : (
          <span className="hint net-family-note">
            {mode === 'auto'
              ? 'The daemon chooses a block nothing on the host uses.'
              : `No IPv${family} addresses on this network.`}
          </span>
        )}
      </div>

      {mode === 'custom' && (
        <>
          {error ? (
            <span className="field-error">{error}</span>
          ) : block && (
            <span className="hint subnet-preview">
              Bridge <span className="mono">{formatAddress(block.gateway, family)}</span>
              {' · '}{count(hostCount(block), bits - block.prefix)} addresses for instances
              {family === 4 && hostCount(block) > 0n && (
                <>
                  {' · usable '}
                  <span className="mono">
                    {formatAddress(block.network + 1n, 4)} – {formatAddress(
                      block.network + (1n << BigInt(bits - block.prefix)) - 2n, 4)}
                  </span>
                </>
              )}
              {!isPrivate(block) && (
                <span className="subnet-warn">
                  {' '}Not a private range: instances would shadow those addresses on the internet.
                </span>
              )}
              {family === 6 && block.prefix !== 64 && (
                <span className="subnet-warn">
                  {' '}Stateless addressing needs a /64; other sizes need
                  {' '}<span className="mono">ipv6.dhcp.stateful=true</span>.
                </span>
              )}
            </span>
          )}
          {others.length > 0 && (
            <span className="hint">
              Already on this host:{' '}
              {others.map((entry, index) => (
                <span key={`${entry.interface}-${entry.subnet}`}>
                  {index > 0 && ', '}
                  <span className="mono">{entry.subnet}</span> ({entry.interface})
                </span>
              ))}
            </span>
          )}
        </>
      )}
    </div>
  )
}
