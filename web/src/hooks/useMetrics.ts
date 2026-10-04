import { useEffect, useRef, useState } from 'react'
import { api } from '../lib/api'
import type { InstanceMetrics, MetricPoint, NodeMetrics } from '../lib/types'

/** What the page holds for one node: every point it has been given, within the window. */
export interface NodeHistory {
  node: string
  self: boolean
  /** Why the latest poll got nothing from it; its history stays on screen meanwhile. */
  error: string | null
  period: number
  cpuThreads: number
  memoryTotal: number
  uplink: string
  at: number | null
  host: MetricPoint[]
  instances: InstanceMetrics[]
}

interface Options {
  /** Seconds of history to hold and to ask for on the first poll. */
  window: number
  nodes?: string[]
  groups?: string[]
  paused?: boolean
  /** How often to ask, in seconds. Nodes sample every 2s whatever this is. */
  every?: number
}

const RETRY_MS = 4000

function sleep(ms: number, signal: AbortSignal) {
  return new Promise<void>((resolve) => {
    const timer = window.setTimeout(resolve, ms)
    signal.addEventListener('abort', () => { window.clearTimeout(timer); resolve() })
  })
}

/** Resolves once the page is visible: a hidden tab asks nobody for numbers it cannot show. */
function visible(signal: AbortSignal) {
  if (document.visibilityState !== 'hidden') return Promise.resolve()
  return new Promise<void>((resolve) => {
    const done = () => { document.removeEventListener('visibilitychange', check); resolve() }
    const check = () => { if (document.visibilityState !== 'hidden') done() }
    document.addEventListener('visibilitychange', check)
    signal.addEventListener('abort', done)
  })
}

function trim(points: MetricPoint[], floor: number) {
  let i = 0
  while (i < points.length && points[i][0] < floor) i += 1
  return i ? points.slice(i) : points
}

/** One node's history with the latest answer folded in. */
function merge(previous: NodeHistory | undefined, answer: NodeMetrics, window: number): NodeHistory {
  if (!answer.ok || !answer.host) {
    return previous ? { ...previous, error: answer.error ?? 'no answer' } : {
      node: answer.node, self: answer.self, error: answer.error ?? 'no answer', period: 2,
      cpuThreads: 0, memoryTotal: 0, uplink: '', at: null, host: [], instances: [],
    }
  }
  const at = answer.at ?? previous?.at ?? null
  const floor = (at ?? 0) - window
  const before = new Map((previous?.instances ?? []).map((i) => [i.name, i.points]))
  return {
    node: answer.node,
    self: answer.self,
    error: null,
    period: answer.period ?? 2,
    cpuThreads: answer.host.cpu_threads,
    memoryTotal: answer.host.memory_total,
    uplink: answer.host.uplink,
    at,
    host: trim([...(previous?.host ?? []), ...answer.host.points], floor),
    // The answer lists every instance there now, so one missing from it is
    // gone and takes its history with it, as it does on the node.
    instances: (answer.instances ?? []).map((i) => ({
      ...i, points: trim([...(before.get(i.name) ?? []), ...i.points], floor),
    })),
  }
}

/**
 * Every node's usage history, kept current: the window on the first poll,
 * then only what each node sampled since (the cursor holds each one's place).
 * Pausing stops asking and keeps what is on screen; resuming catches up on
 * what was sampled meanwhile. Another window or scope starts over.
 */
export function useMetrics({ window, nodes, groups, paused = false, every = 2 }: Options) {
  const [history, setHistory] = useState<NodeHistory[]>([])
  const [error, setError] = useState<string | null>(null)
  const [live, setLive] = useState(false)
  const key = JSON.stringify([window, nodes ?? null, groups ?? null])
  // Where the next poll asks from, and for which scope: a cursor from another
  // window would skip the history this one wants.
  const cursor = useRef<{ key: string; value?: string }>({ key })
  const [shownKey, setShownKey] = useState(key)

  if (shownKey !== key) {
    // Adjusted while rendering, so another scope's lines never show under this one.
    setShownKey(key)
    setHistory([])
  }

  useEffect(() => {
    if (paused) return
    const controller = new AbortController()
    const { signal } = controller
    const [win, onlyNodes, onlyGroups] = JSON.parse(key) as [number, string[] | null, string[] | null]
    if (cursor.current.key !== key) cursor.current = { key }

    async function poll() {
      while (!signal.aborted) {
        await visible(signal)
        if (signal.aborted) return
        const started = Date.now()
        try {
          const page = await api.clusterMetrics({
            cursor: cursor.current.value, window: win, nodes: onlyNodes ?? undefined, groups: onlyGroups ?? undefined,
          }, signal)
          if (signal.aborted) return
          cursor.current = { key, value: page.cursor }
          setHistory((current) => {
            const known = new Map(current.map((h) => [h.node, h]))
            return page.nodes.map((answer) => merge(known.get(answer.node), answer, win))
          })
          setError(null)
          setLive(true)
          await sleep(Math.max(250, every * 1000 - (Date.now() - started)), signal)
        } catch (e) {
          if (signal.aborted) return
          setError((e as Error).message)
          setLive(false)
          await sleep(RETRY_MS, signal)
        }
      }
    }
    poll()
    return () => { controller.abort(); setLive(false) }
  // A new rate restarts the loop but keeps the cursor: nothing is fetched twice.
  }, [key, paused, every])

  return { history, error, live }
}
