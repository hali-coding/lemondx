import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../lib/api'
import type { LogEvent, LogFilters } from '../lib/types'

/** Where a tail reads from: the cluster (some or all nodes), or one node. */
export type TailScope =
  | { kind: 'cluster'; nodes?: string[]; group?: string }
  | { kind: 'node'; node?: string }

interface Options {
  scope: TailScope
  filters: LogFilters
  paused?: boolean
  /** Events kept in the page; the oldest go first. */
  max?: number
  /** How many recent events a tail opens with. */
  backlog?: number
}

// The server holds each poll open this long when nothing happens, so a tail
// costs one request per 20 seconds when the cluster is quiet.
const WAIT = 20
const RETRY_MS = 3000

function sleep(ms: number, signal: AbortSignal) {
  return new Promise<void>((resolve) => {
    const timer = window.setTimeout(resolve, ms)
    signal.addEventListener('abort', () => { window.clearTimeout(timer); resolve() })
  })
}

/** Resolves once the page is visible: a hidden tab holds none of the browser's
 *  few connections per host open for a tail nobody is reading. */
function visible(signal: AbortSignal) {
  if (document.visibilityState !== 'hidden') return Promise.resolve()
  return new Promise<void>((resolve) => {
    const done = () => { document.removeEventListener('visibilitychange', check); resolve() }
    const check = () => { if (document.visibilityState !== 'hidden') done() }
    document.addEventListener('visibilitychange', check)
    signal.addEventListener('abort', done)
  })
}

/**
 * A live tail of lemondx's events: the backlog first, then each new event as
 * it happens, by long-polling `/api/logs` or `/api/cluster/logs` with the
 * cursor the last answer gave. Pausing keeps the cursor, so resuming catches
 * up on what happened meanwhile (as far as each node's buffer reaches); a
 * change of scope or filters starts over.
 */
export function useLogTail({ scope, filters, paused = false, max = 2000, backlog = 200 }: Options) {
  const [events, setEvents] = useState<LogEvent[]>([])
  const [notices, setNotices] = useState<string[]>([])
  const [error, setError] = useState<string | null>(null)
  const [live, setLive] = useState(false)
  // Where the next poll asks from, and for which scope and filters: a cursor
  // from other filters would skip events these ones want.
  const cursor = useRef<{ key: string; value?: string }>({ key: '' })
  const key = JSON.stringify([scope, filters])
  const [shownKey, setShownKey] = useState(key)

  if (shownKey !== key) {
    // Adjusted while rendering, so the old scope's rows never show under new filters.
    setShownKey(key)
    setEvents([])
    setNotices([])
  }

  useEffect(() => {
    if (paused) return
    if (cursor.current.key !== key) cursor.current = { key }
    const controller = new AbortController()
    const { signal } = controller
    const parsed = JSON.parse(key) as [TailScope, LogFilters]
    const [where, wanted] = parsed

    async function poll() {
      while (!signal.aborted) {
        await visible(signal)
        if (signal.aborted) return
        try {
          const first = cursor.current.value === undefined
          const wait = first ? 0 : WAIT
          let fresh: LogEvent[]
          const problems: string[] = []
          if (where.kind === 'cluster') {
            const page = await api.clusterLogs({ ...wanted, cursor: cursor.current.value, wait,
              limit: first ? backlog : 500, nodes: where.nodes, group: where.group }, signal)
            cursor.current = { key, value: page.cursor }
            fresh = page.events
            for (const node of page.nodes) {
              if (!node.ok) problems.push(`${node.node}: ${node.error ?? 'unreachable'}`)
              else if (node.reset && !first) problems.push(`${node.node} restarted; showing from then`)
              else if (node.truncated) problems.push(`${node.node}: some events were missed (more than its buffer holds)`)
            }
          } else {
            const page = await api.logs({ ...wanted, after: cursor.current.value, wait,
              limit: first ? backlog : 500 }, where.node, signal)
            cursor.current = { key, value: page.cursor }
            fresh = page.events.map((e) => ({ ...e, node: where.node ?? e.node }))
            if (page.reset && !first) problems.push('the node restarted; showing from then')
            if (page.truncated) problems.push('some events were missed (more than the buffer holds)')
          }
          setError(null)
          setLive(true)
          if (fresh.length) setEvents((current) => [...current, ...fresh].slice(-max))
          if (problems.length) {
            setNotices((current) => [...new Set([...current, ...problems])].slice(-5))
          }
        } catch (cause) {
          if (signal.aborted) return
          setLive(false)
          setError((cause as Error).message)
          await sleep(RETRY_MS, signal)
        }
      }
    }
    void poll()
    return () => controller.abort()
  }, [key, paused, max, backlog])

  const clear = useCallback(() => { setEvents([]); setNotices([]) }, [])
  return { events, notices, error, live, clear }
}
