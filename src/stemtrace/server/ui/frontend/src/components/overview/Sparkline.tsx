import { clsx } from 'clsx'
import type { ReactElement } from 'react'
import type { Grade } from './model'

interface SparklineProps {
  /** One value per bucket, oldest first; null is a bucket with nothing to draw: it breaks the line (never bridged, never a zero). */
  values: readonly (number | null)[]
  /** Fixed pixel width; none fills the container. */
  width?: number
  height: number
  grade?: Grade
}

const TONE: Record<Grade, string> = {
  ok: 'text-muted',
  warn: 'text-retry-t',
  bad: 'text-failure-t',
}

/**
 * A line with a faint area under it, scaled to its own maximum (the prototype's `spark`). The
 * tone follows the tile's grade; it is decoration, so the numbers beside it carry the meaning.
 */
export function Sparkline({
  values,
  width,
  height,
  grade = 'ok',
}: SparklineProps): ReactElement | null {
  if (values.length === 0) return null
  const w = width ?? 100
  // A null bucket breaks the line: each run of buckets with a value is a segment of its own
  const xOf = (i: number): number =>
    (values.length === 1 ? 0 : (i * (w - 2)) / (values.length - 1)) + 1
  const segments: { x: number; y: number }[][] = []
  let max = 0
  values.forEach((v, i) => {
    if (v === null) return
    max = Math.max(max, v)
    if (values[i - 1] == null) segments.push([])
    segments[segments.length - 1].push({ x: xOf(i), y: v })
  })
  if (segments.length === 0) return null
  const yOf = (v: number): number => height - 2 - (v / (max || 1)) * (height - 4)
  const textOf = (seg: { x: number; y: number }[]): string =>
    seg.map((p) => `${p.x.toFixed(1)},${yOf(p.y).toFixed(1)}`).join(' ')
  return (
    <svg
      aria-hidden="true"
      width={width ?? '100%'}
      height={height}
      viewBox={`0 0 ${w} ${height}`}
      preserveAspectRatio="none"
      className={clsx('block overflow-visible', TONE[grade])}
    >
      {segments.map((seg) => {
        const first = seg[0]
        if (seg.length === 1) {
          return (
            <circle key={first.x} cx={first.x} cy={yOf(first.y)} r={1.75} fill="currentColor" />
          )
        }
        const last = seg[seg.length - 1]
        return (
          <g key={first.x}>
            <polygon
              points={`${first.x.toFixed(1)},${height} ${textOf(seg)} ${last.x.toFixed(1)},${height}`}
              fill="currentColor"
              opacity={0.14}
            />
            <polyline
              points={textOf(seg)}
              fill="none"
              stroke="currentColor"
              strokeWidth={1.75}
              vectorEffect="non-scaling-stroke"
              strokeLinejoin="round"
              strokeLinecap="round"
            />
          </g>
        )
      })}
    </svg>
  )
}
