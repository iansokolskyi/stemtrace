import { clsx } from 'clsx'
import type { ReactElement, ReactNode } from 'react'

interface SkeletonProps {
  /** A card-sized block (surface fill, 8 px corners) instead of a line or chip. */
  card?: boolean
  className?: string
}

/** The one loading placeholder: a pulsing block. Shape and size come from the caller. */
export function Skeleton({ card, className }: SkeletonProps): ReactElement {
  return (
    <div
      data-skeleton-block
      aria-hidden="true"
      className={clsx(
        'animate-pulse',
        card ? 'rounded-lg bg-surface' : 'rounded-[3px] bg-raised',
        className,
      )}
    />
  )
}

interface SkeletonRegionProps {
  /** What is loading, as it reads after "Loading": "executions". */
  label: string
  className?: string
  children: ReactNode
}

/** Wraps a content-shaped arrangement of `Skeleton`s and tells assistive technology what loads. */
export function SkeletonRegion({ label, className, children }: SkeletonRegionProps): ReactElement {
  return (
    <div data-testid="skeleton" role="status" aria-label={`Loading ${label}`} className={className}>
      {children}
    </div>
  )
}
