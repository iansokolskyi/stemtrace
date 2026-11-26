import type { ReactElement } from 'react'
import { clock } from '@/components/triage/runsScope'
import { RetryButton } from './LoadError'

/** A refresh failed but the page still shows what loaded before: say how old it is. */
export function StaleNotice({
  since,
  retrying,
  onRetry,
}: {
  /** When the oldest part that failed to refresh last had data (ms). */
  since: number
  retrying: boolean
  onRetry: () => unknown
}): ReactElement {
  return (
    <p role="status" className="m-0 mb-4 flex flex-wrap items-center gap-x-2 text-xs text-muted">
      <span>
        Couldn&apos;t refresh. Showing data from {clock(new Date(since).toISOString())} UTC.
      </span>
      <RetryButton retry={onRetry} busy={retrying} />
    </p>
  )
}
