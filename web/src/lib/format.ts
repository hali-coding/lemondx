export function bytes(value: number | null | undefined): string {
  const n = Number(value ?? 0)
  if (!n || n <= 0) return '—'
  const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB']
  let size = n
  let unit = 0
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024
    unit += 1
  }
  return `${unit === 0 ? size.toFixed(0) : size.toFixed(1)} ${units[unit]}`
}

export function relativeTime(iso: string | null | undefined): string {
  if (!iso) return '—'
  const then = new Date(iso).getTime()
  if (Number.isNaN(then)) return '—'
  // LXD uses a zero timestamp for "never".
  if (then <= 0 || new Date(iso).getUTCFullYear() < 1980) return 'never'

  const seconds = Math.round((Date.now() - then) / 1000)
  if (seconds < 60) return 'just now'
  const steps: [number, Intl.RelativeTimeFormatUnit][] = [
    [60, 'minute'], [24, 'hour'], [7, 'day'], [4.345, 'week'], [12, 'month'],
  ]
  let value = seconds / 60
  let unit: Intl.RelativeTimeFormatUnit = 'minute'
  for (const [divisor, nextUnit] of steps.slice(1)) {
    if (Math.abs(value) < divisor) break
    value /= divisor
    unit = nextUnit
  }
  const formatter = new Intl.RelativeTimeFormat(undefined, { numeric: 'auto' })
  return formatter.format(-Math.round(value), unit)
}

export function absoluteTime(iso: string | null | undefined): string {
  if (!iso) return '—'
  const date = new Date(iso)
  if (Number.isNaN(date.getTime()) || date.getUTCFullYear() < 1980) return '—'
  return date.toLocaleString()
}

/** CPU seconds consumed, from LXD's nanosecond counter. */
export function cpuTime(nanoseconds: number): string {
  const seconds = (nanoseconds ?? 0) / 1e9
  if (seconds <= 0) return '—'
  if (seconds < 60) return `${seconds.toFixed(1)}s`
  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) return `${minutes}m ${Math.floor(seconds % 60)}s`
  return `${Math.floor(minutes / 60)}h ${minutes % 60}m`
}

/** "12s ago" for an epoch in seconds, as the health checks report times. */
export function secondsAgo(epoch: number | null | undefined): string {
  if (!epoch) return 'never'
  const seconds = Math.max(0, Math.round(Date.now() / 1000 - epoch))
  if (seconds < 60) return `${seconds}s ago`
  if (seconds < 3600) return `${Math.round(seconds / 60)} min ago`
  return `${Math.round(seconds / 3600)} h ago`
}
