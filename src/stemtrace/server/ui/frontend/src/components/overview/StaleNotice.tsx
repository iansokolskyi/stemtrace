import type { ReactElement } from 'react'
import { clock } from '@/components/triage/runsScope'
import { RetryButton } from './LoadError'

/** A refresh failed but the page still shows what loaded before: say how old it is. */
export function StaleNotice({
  since,
  onRetry,
}: {
  /** The oldest `dataUpdatedAt` (ms) among the parts that failed to refresh. */
  since: number
  onRetry: () => unknown
}): ReactElement {
  return (
    <p role="status" className="m-0 mb-4 flex flex-wrap items-center gap-x-2 text-xs text-muted">
      <span>
        Couldn&apos;t refresh. Showing data from {clock(new Date(since).toISOString())} UTC.
      </span>
      <RetryButton retry={onRetry} />
    </p>
  )
}
