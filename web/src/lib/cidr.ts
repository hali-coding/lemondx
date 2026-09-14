import type { SubnetInUse } from './types'

export type Family = 4 | 6

export interface Block {
  family: Family
  /** The network address, as an integer. */
  network: bigint
  prefix: number
  /** The bridge's own address: the one typed, or the block's first host. */
  gateway: bigint
}

const BITS: Record<Family, number> = { 4: 32, 6: 128 }

function parseV4(text: string): bigint | null {
  const parts = text.split('.')
  if (parts.length !== 4) return null
  let value = 0n
  for (const part of parts) {
    if (!/^\d{1,3}$/.test(part) || Number(part) > 255) return null
    value = (value << 8n) | BigInt(part)
  }
  return value
}

function parseV6(text: string): bigint | null {
  const halves = text.toLowerCase().split('::')
  if (halves.length > 2) return null
  const groups = (half: string) => (half ? half.split(':') : [])
  const head = groups(halves[0])
  const tail = halves.length === 2 ? groups(halves[1]) : []
  const missing = 8 - head.length - tail.length
  if (halves.length === 1 ? missing !== 0 : missing < 1) return null
  let value = 0n
  for (const group of [...head, ...Array(halves.length === 2 ? missing : 0).fill('0'), ...tail]) {
    if (!/^[0-9a-f]{1,4}$/.test(group)) return null
    value = (value << 16n) | BigInt(parseInt(group, 16))
  }
  return value
}

export function formatAddress(value: bigint, family: Family): string {
  if (family === 4) {
    return [24n, 16n, 8n, 0n].map((shift) => String((value >> shift) & 255n)).join('.')
  }
  const groups = [...Array(8)].map((_, i) => Number((value >> BigInt((7 - i) * 16)) & 0xffffn))
  // RFC 5952, as Python writes it: the longest run of two or more zero groups
  // becomes "::", the first one on a tie.
  let best = -1
  let bestLength = 1
  for (let i = 0; i < 8;) {
    if (groups[i] !== 0) { i++; continue }
    let j = i
    while (j < 8 && groups[j] === 0) j++
    if (j - i > bestLength) { best = i; bestLength = j - i }
    i = j
  }
  const hex = groups.map((g) => g.toString(16))
  if (best < 0) return hex.join(':')
  return `${hex.slice(0, best).join(':')}::${hex.slice(best + bestLength).join(':')}`
}

/** A CIDR block as typed, or why it is not one. */
export function parseBlock(text: string, family: Family): Block | string {
  const example = family === 4 ? '10.20.0.0/24' : 'fd42:20::/64'
  const [address, prefixText, extra] = text.trim().split('/')
  if (!address) return `Enter a CIDR block, e.g. ${example}.`
  if (prefixText === undefined) return `Add a prefix length, e.g. ${example}.`
  const bits = BITS[family]
  const value = family === 4 ? parseV4(address) : parseV6(address)
  if (extra !== undefined || value === null || !/^\d{1,3}$/.test(prefixText)) {
    return `Not an IPv${family} CIDR block, e.g. ${example}.`
  }
  const prefix = Number(prefixText)
  if (prefix < 1 || prefix > bits) return `The prefix must be between 1 and ${bits}.`
  const size = 1n << BigInt(bits - prefix)
  if (size < 4n) return `A /${prefix} is too small to hand out addresses.`
  const network = value & ~(size - 1n) & ((1n << BigInt(bits)) - 1n)
  if (family === 4 && value === network + size - 1n) {
    return `${address} is the broadcast address; the bridge needs a host address.`
  }
  return { family, network, prefix, gateway: value === network ? network + 1n : value }
}

export function blockText(block: Block): string {
  return `${formatAddress(block.network, block.family)}/${block.prefix}`
}

/** What the daemon stores: the bridge's own address with the prefix. */
export function bridgeAddress(block: Block): string {
  return `${formatAddress(block.gateway, block.family)}/${block.prefix}`
}

/** How the field shows a stored address: as a block unless the gateway is unusual. */
export function displayAddress(stored: string, family: Family): string {
  const block = parseBlock(stored, family)
  if (typeof block === 'string') return stored
  return block.gateway === block.network + 1n ? blockText(block) : stored
}

export function hostCount(block: Block): bigint {
  const size = 1n << BigInt(BITS[block.family] - block.prefix)
  // IPv4 loses the network and broadcast addresses, both lose the gateway.
  return size - (block.family === 4 ? 3n : 2n)
}

export function overlapping(block: Block, used: SubnetInUse[]): SubnetInUse[] {
  const bits = BITS[block.family]
  const end = block.network + (1n << BigInt(bits - block.prefix)) - 1n
  return used.filter((entry) => {
    if (entry.family !== block.family) return false
    const other = parseBlock(entry.subnet, block.family)
    if (typeof other === 'string') return false
    const otherEnd = other.network + (1n << BigInt(bits - other.prefix)) - 1n
    return block.network <= otherEnd && other.network <= end
  })
}

export function isPrivate(block: Block): boolean {
  const within = (text: string) => {
    const range = parseBlock(text, block.family)
    if (typeof range === 'string') return false
    const shift = BigInt(BITS[block.family] - range.prefix)
    return block.prefix >= range.prefix && (block.network >> shift) === (range.network >> shift)
  }
  return block.family === 4
    ? ['10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '100.64.0.0/10'].some(within)
    : within('fc00::/7')
}

/** A /24 (or /64) nothing on the host overlaps, to start the field from. */
export function suggestBlock(family: Family, used: SubnetInUse[]): string {
  for (let n = 20; n < 256; n++) {
    const candidate = family === 4 ? `10.${n}.0.0/24` : `fd42:${n.toString(16)}::/64`
    const block = parseBlock(candidate, family)
    if (typeof block !== 'string' && overlapping(block, used).length === 0) return candidate
  }
  return family === 4 ? '10.20.0.0/24' : 'fd42:20::/64'
}

/**
 * What a typed value means for the daemon: the address to send, or why it
 * cannot be sent. Parents use it to gate submit, so the field and the button
 * cannot disagree.
 */
export function subnetStatus(value: string, family: Family, used: SubnetInUse[] | null) {
  if (value === 'auto' || value === 'none') return { address: value, error: null, block: null }
  const block = parseBlock(value, family)
  if (typeof block === 'string') return { address: null, error: block, block: null }
  const clash = overlapping(block, used ?? [])
  if (clash.length > 0) {
    return {
      address: null,
      block,
      error: `${blockText(block)} overlaps ${clash.map((c) => `${c.subnet} on ${c.interface}`)
        .join(', ')}. Pick another block.`,
    }
  }
  return { address: bridgeAddress(block), error: null, block }
}
