import { clsx } from 'clsx'
import type { ReactElement } from 'react'
import type { Grade } from './model'

interface SparklineProps {
  /** One value per bucket, oldest first; null is a bucket with nothing to draw (a gap, not a zero). */
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
  const drawn = values.flatMap((v, i) => (v === null ? [] : [{ v, i }]))
  if (drawn.length === 0) return null
  const max = Math.max(...drawn.map((d) => d.v)) || 1
  const points = drawn.map(({ v, i }) => {
    const x = (values.length === 1 ? 0 : (i * (w - 2)) / (values.length - 1)) + 1
    return `${x.toFixed(1)},${(height - 2 - (v / max) * (height - 4)).toFixed(1)}`
  })
  return (
    <svg
      aria-hidden="true"
      width={width ?? '100%'}
      height={height}
      viewBox={`0 0 ${w} ${height}`}
      preserveAspectRatio="none"
      className={clsx('block overflow-visible', TONE[grade])}
    >
      <polygon
        points={`1,${height} ${points.join(' ')} ${w - 1},${height}`}
        fill="currentColor"
        opacity={0.14}
      />
      <polyline
        points={points.join(' ')}
        fill="none"
        stroke="currentColor"
        strokeWidth={1.75}
        vectorEffect="non-scaling-stroke"
        strokeLinejoin="round"
        strokeLinecap="round"
      />
    </svg>
  )
}
