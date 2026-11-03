import { clsx } from 'clsx'
import type { ReactElement } from 'react'
import type { Grade } from './model'

interface SparklineProps {
  /** One value per bucket, oldest first. */
  values: readonly number[]
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
  const max = Math.max(...values) || 1
  const points = values.map((v, i) => {
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
