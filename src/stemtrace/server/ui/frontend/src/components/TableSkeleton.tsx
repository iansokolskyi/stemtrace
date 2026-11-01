import type { ReactElement } from 'react'

/** Three placeholder rows while a list loads. */
export function TableSkeleton(): ReactElement {
  return (
    <div
      aria-hidden="true"
      className="animate-pulse space-y-px overflow-hidden rounded-lg bg-surface"
    >
      {[0, 1, 2].map((i) => (
        <div key={i} className="h-9 border-t border-rule first:border-t-0" />
      ))}
    </div>
  )
}
