import { clsx } from 'clsx'
import type { ReactElement } from 'react'
import { Skeleton, SkeletonRegion } from './Skeleton'

const WIDTHS = ['w-16', 'w-48', 'w-24', 'w-20', 'w-12']

interface TableSkeletonProps {
  /** What is loading, as it reads after "Loading": "workers". */
  label: string
  /** Rows under the header row. */
  rows?: number
  /** No box of its own: it sits inside a panel that has one. */
  bare?: boolean
}

/** A header row and body rows with the columns' rhythm, while a list loads. */
export function TableSkeleton({ label, rows = 5, bare }: TableSkeletonProps): ReactElement {
  return (
    <SkeletonRegion
      label={label}
      className={clsx(!bare && 'overflow-hidden rounded-lg bg-surface')}
    >
      {Array.from({ length: rows + 1 }, (_, row) => (
        <div
          key={row}
          data-testid="skeleton-row"
          className={clsx('flex h-9 items-center gap-6 px-4', row > 0 && 'border-t border-rule')}
        >
          {WIDTHS.map((width, col) => (
            <Skeleton
              key={width}
              className={clsx('h-3', width, col === 1 && 'grow', row === 0 && 'h-2.5')}
            />
          ))}
        </div>
      ))}
    </SkeletonRegion>
  )
}
