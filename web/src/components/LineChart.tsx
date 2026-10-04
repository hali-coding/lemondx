import { useEffect, useMemo, useRef, useState } from 'react'
import type { PointerEvent } from 'react'

/** One line: `[seconds, value]` pairs, oldest first; a null value is a gap. */
export interface Series {
  key: string
  label: string
  /** A CSS color, normally one of the `--series-n` tokens. */
  color: string
  points: [number, number | null][]
}

interface Props {
  series: Series[]
  /** The time axis, in seconds: every chart on a page shares one. */
  start: number
  end: number
  /** A fixed top for the scale (100 for a share); otherwise it follows the data. */
  max?: number
  /** Scale steps in powers of 1024 rather than 10, so byte ticks land on round units. */
  binary?: boolean
  format: (value: number) => string
  /** Further apart than this, two points are not joined: nothing was sampled between. */
  gap: number
  height?: number
  /** A crosshair shared between charts; uncontrolled when not given. */
  hoverAt?: number | null
  onHover?: (at: number | null) => void
  label: string
}

const LEFT = 52
const RIGHT = 8
const TOP = 8
const BOTTOM = 20

/** The smallest 1-2-5 step at or above `value`, in the scale's own base. */
function niceCeil(value: number, binary = false) {
  if (!(value > 0)) return 1
  let unit = 1
  if (binary) while (value / unit >= 1024) unit *= 1024
  const scaled = value / unit
  const magnitude = 10 ** Math.floor(Math.log10(scaled))
  const step = [1, 2, 2.5, 5, 10].find((s) => s * magnitude >= scaled) ?? 10
  return step * magnitude * unit
}

/** The point nearest `at`, by binary search, or null if none is within `gap`. */
function nearest(points: [number, number | null][], at: number, gap: number) {
  let lo = 0
  let hi = points.length - 1
  if (hi < 0) return null
  while (lo < hi) {
    const mid = (lo + hi) >> 1
    if (points[mid][0] < at) lo = mid + 1
    else hi = mid
  }
  const candidates = [points[lo], points[lo - 1]].filter(Boolean)
  const best = candidates.reduce((a, b) => (Math.abs(b[0] - at) < Math.abs(a[0] - at) ? b : a))
  return Math.abs(best[0] - at) <= gap ? best : null
}

function clock(seconds: number, withSeconds = false) {
  return new Date(seconds * 1000).toLocaleTimeString([], {
    hour: '2-digit', minute: '2-digit', ...(withSeconds ? { second: '2-digit' } : {}),
  })
}

/** Width of the element, following resizes. */
function useWidth() {
  const ref = useRef<HTMLDivElement>(null)
  const [width, setWidth] = useState(0)
  useEffect(() => {
    const element = ref.current
    if (!element) return
    const observer = new ResizeObserver(([entry]) => setWidth(Math.floor(entry.contentRect.width)))
    observer.observe(element)
    return () => observer.disconnect()
  }, [])
  return [ref, width] as const
}

/**
 * Lines over time with a crosshair: the pointer picks a moment, and the
 * readout lists every line's value then, so nobody has to land on a 2px line.
 * Drawn by hand in SVG, since the UI carries no chart library.
 */
export function LineChart({
  series, start, end, max, binary = false, format, gap, height = 150,
  hoverAt, onHover, label,
}: Props) {
  const [ref, width] = useWidth()
  const [ownHover, setOwnHover] = useState<number | null>(null)
  const controlled = onHover !== undefined
  const at = controlled ? (hoverAt ?? null) : ownHover
  const setAt = controlled ? onHover : setOwnHover

  const plotW = Math.max(10, width - LEFT - RIGHT)
  const plotH = height - TOP - BOTTOM
  const span = Math.max(1, end - start)

  const top = useMemo(() => {
    if (max !== undefined) return max
    let highest = 0
    for (const s of series) {
      for (const [t, v] of s.points) if (t >= start && v !== null && v > highest) highest = v
    }
    return niceCeil(highest * 1.08, binary)
  }, [series, start, max, binary])

  const x = (t: number) => LEFT + ((t - start) / span) * plotW
  const y = (v: number) => TOP + plotH - (Math.min(v, top) / top) * plotH

  const paths = useMemo(() => series.map((s) => {
    let d = ''
    let previous: number | null = null
    for (const [t, v] of s.points) {
      if (t < start - gap || v === null) { previous = null; continue }
      const command = previous === null || t - previous > gap ? 'M' : 'L'
      d += `${command}${x(t).toFixed(1)},${y(v).toFixed(1)}`
      previous = t
    }
    return d
    // x and y are pure functions of these.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }), [series, start, span, plotW, plotH, top, gap])

  const ticks = [0, top / 2, top]
  // Four time labels, or two when narrow; the right-hand one is "now".
  const times = (width < 360 ? [0, 1] : [0, 1 / 3, 2 / 3, 1]).map((f) => start + f * span)

  function pick(event: PointerEvent<SVGRectElement>) {
    const box = event.currentTarget.getBoundingClientRect()
    const fraction = Math.min(1, Math.max(0, (event.clientX - box.left) / box.width))
    setAt(start + fraction * span)
  }

  const readout = at === null ? [] : series.map((s) => ({ s, point: nearest(s.points, at, gap) }))
  const shownAt = readout.find((r) => r.point)?.point?.[0] ?? at
  const tipLeft = at === null ? 0 : x(at)
  const flip = tipLeft > width / 2

  return (
    <div className="chart" ref={ref} style={{ height }}>
      {width > 0 && (
        <svg width={width} height={height} role="img" aria-label={label}>
          {ticks.map((v) => (
            <g key={v}>
              <line className="chart-grid" x1={LEFT} x2={LEFT + plotW} y1={y(v)} y2={y(v)} />
              <text className="chart-tick" x={LEFT - 6} y={y(v) + 3.5} textAnchor="end">
                {format(v)}
              </text>
            </g>
          ))}
          {times.map((t, i) => (
            <text key={i} className="chart-tick" x={x(t)} y={height - 5}
              textAnchor={i === 0 ? 'start' : i === times.length - 1 ? 'end' : 'middle'}>
              {clock(t)}
            </text>
          ))}
          {paths.map((d, i) => d && (
            <path key={series[i].key} d={d} className="chart-line"
              style={{ stroke: series[i].color }} />
          ))}
          {at !== null && (
            <>
              <line className="chart-cross" x1={x(at)} x2={x(at)} y1={TOP} y2={TOP + plotH} />
              {readout.map(({ s, point }) => point && point[1] !== null && (
                <circle key={s.key} cx={x(point[0])} cy={y(point[1])} r={3.5}
                  className="chart-dot" style={{ fill: s.color }} />
              ))}
            </>
          )}
          <rect x={LEFT} y={TOP} width={plotW} height={plotH} fill="transparent"
            onPointerMove={pick} onPointerDown={pick} onPointerLeave={() => setAt(null)} />
        </svg>
      )}
      {at !== null && shownAt !== null && readout.some((r) => r.point) && (
        <div className="chart-tip" style={flip
          ? { right: width - tipLeft + 10 } : { left: tipLeft + 10 }}>
          <div className="chart-tip-time">{clock(shownAt, true)}</div>
          {readout.map(({ s, point }) => (
            <div key={s.key} className="chart-tip-row">
              <span className="chart-key" style={{ background: s.color }} />
              <strong>{point && point[1] !== null ? format(point[1]) : '—'}</strong>
              <span className="chart-tip-label">{s.label}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

/** A trend at a glance, for a table cell: one line, no axes. */
export function Sparkline({ points, start, end, gap, floor = 0, color = 'var(--series-1)' }: {
  points: [number, number | null][]
  start: number
  end: number
  gap: number
  /** The least the scale reaches, so an idle line lies flat rather than magnifying noise. */
  floor?: number
  color?: string
}) {
  const w = 84
  const h = 22
  const span = Math.max(1, end - start)
  let top = floor
  for (const [t, v] of points) if (t >= start && v !== null && v > top) top = v
  top = top || 1
  let d = ''
  let previous: number | null = null
  for (const [t, v] of points) {
    if (t < start || v === null) { previous = null; continue }
    const command = previous === null || t - previous > gap ? 'M' : 'L'
    d += `${command}${(((t - start) / span) * w).toFixed(1)},${(h - 1 - (v / top) * (h - 2)).toFixed(1)}`
    previous = t
  }
  return (
    <svg className="sparkline" width={w} height={h} aria-hidden>
      <path d={d} style={{ stroke: color }} />
    </svg>
  )
}
