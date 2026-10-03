import type { ImageBrowse, ImagePin } from './types'

/** The catalog entry a pin was made from: same remote, architecture and one of its names. */
export function entryFor(pin: ImagePin, browse: ImageBrowse | null) {
  return browse?.entries.find((e) => e.remote === pin.remote && e.arch === pin.arch
    && pin.aliases.includes(e.alias)) ?? null
}

/**
 * The pin `image` names, mirroring `ContainerService.find_pin()`: `pin:` and
 * an id, or else a nickname. Any other image is no pin, whatever is pinned.
 */
export function pinFor(image: string, pins: ImagePin[]) {
  const text = image.trim()
  if (!text.startsWith('pin:')) return null
  const ref = text.slice(4)
  return pins.find((p) => p.name === ref) ?? pins.find((p) => p.nicknames.includes(ref)) ?? null
}

/** How a picker names a pin: its first nickname, which reads better than the id. */
export function pinRef(pin: ImagePin) {
  return `pin:${pin.nicknames[0] ?? pin.name}`
}

/**
 * A build's serial short enough for a badge: its date where it starts with
 * one (`20261002_05:24`, `20260926`), else the serial as it is.
 */
export function shortSerial(serial: string) {
  const date = /^(\d{4})(\d{2})(\d{2})/.exec(serial)
  return date ? `${date[1]}-${date[2]}-${date[3]}` : serial
}
