import { useLayoutEffect, useRef } from 'react'
import type { LogEvent } from '../lib/types'

// Shown in their own columns, so not again among the rest.
const OWN_COLUMNS = new Set(['action', 'req', 'actor', 'role', 'via', 'channel', 'origin',
  'origin_channel'])
// Fields that name an instance: clickable, to filter the tail to it.
const INSTANCE_FIELDS = new Set(['instance', 'target', 'name', 'names', 'arg_name', 'new_name'])

function time(ts: number) {
  return new Date(ts * 1000).toLocaleTimeString([], { hour12: false })
}

function text(value: LogEvent['fields'][string]) {
  return Array.isArray(value) ? value.join(', ') : String(value)
}

interface Props {
  events: LogEvent[]
  showNode: boolean
  /** Keep the newest row in view as events arrive, unless scrolled down to read. */
  follow: boolean
  onRequest?: (req: string) => void
  onInstance?: (name: string) => void
  onActor?: (actor: string) => void
  compact?: boolean
  empty?: string
}

/** Events as rows, newest at the top. */
export function LogList({ events, showNode, follow, onRequest, onInstance, onActor, compact,
  empty = 'Nothing yet. New events appear here as they happen.' }: Props) {
  const box = useRef<HTMLDivElement>(null)
  const pinned = useRef(true)
  const height = useRef(0)

  // New rows go in above what is on screen. At the top (following), stay
  // there so they come into view; scrolled down to read, or not following,
  // move down by what was added so the rows being read hold still.
  useLayoutEffect(() => {
    const element = box.current
    if (!element) return
    const added = element.scrollHeight - height.current
    if (follow && pinned.current) element.scrollTop = 0
    else if (added > 0 && element.scrollTop > 0) element.scrollTop += added
    height.current = element.scrollHeight
  }, [events, follow])

  return (
    <div className={`log-list${compact ? ' log-list-compact' : ''}`} ref={box}
      onScroll={(e) => {
        const element = e.currentTarget
        // Scrolled down to read: stop jumping to the top until back there.
        pinned.current = element.scrollTop < 40
      }}>
      {events.length === 0 && <div className="log-empty faint">{empty}</div>}
      {[...events].reverse().map((event) => {
        const f = event.fields
        const actor = f.actor ? String(f.actor) : ''
        const via = [f.channel, f.origin ? `from ${String(f.origin)}` : ''].filter(Boolean).join(' ')
        return (
          <div key={`${event.node}/${event.seq}/${event.ts}`} className={`log-row log-${event.level}`}>
            <span className="log-time mono">{time(event.ts)}</span>
            {showNode && <span className="log-node mono">{event.node}</span>}
            <span className={`log-level log-level-${event.level}`}>{event.level}</span>
            <span className="log-kind faint">{event.kind}</span>
            <span className="log-action mono">{event.action}</span>
            {actor && (
              <button type="button" className="log-actor" title={`Only what ${actor} did`}
                disabled={!onActor} onClick={() => onActor?.(actor)}>
                {actor}{via ? <span className="faint"> · {via}</span> : null}
              </button>
            )}
            <span className="log-fields">
              {Object.entries(f).filter(([key]) => !OWN_COLUMNS.has(key)).map(([key, value]) => (
                INSTANCE_FIELDS.has(key) && onInstance && !Array.isArray(value) ? (
                  <button type="button" key={key} className="log-field log-field-link"
                    title={`Only events naming ${String(value)}`}
                    onClick={() => onInstance(String(value))}>
                    <span className="faint">{key}=</span>{text(value)}
                  </button>
                ) : (
                  <span key={key} className="log-field">
                    <span className="faint">{key}=</span>{text(value)}
                  </span>
                )
              ))}
              {event.source === 'cli' && <span className="log-field faint">via CLI</span>}
            </span>
            {f.req && (
              <button type="button" className="log-req mono" disabled={!onRequest}
                title="Everything this request did, on every node"
                onClick={() => onRequest?.(String(f.req))}>
                {String(f.req)}
              </button>
            )}
          </div>
        )
      })}
    </div>
  )
}
