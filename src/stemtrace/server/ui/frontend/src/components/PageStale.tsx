import type { ReactElement } from 'react'
import { clock } from '@/components/triage/runsScope'
import { staleOf, type ViewState } from '@/hooks/useViewState'
import { RetryButton } from './RetryButton'

/**
 * The page's one quiet line when a refresh failed but the page still shows what loaded before:
 * how old that data is, with Retry. `views` are every query the page renders. Without form login
 * an expired session reads as a request for an API key instead.
 */
export function PageStale({
  views,
  className = 'mb-4',
}: {
  views: ViewState[]
  className?: string
}): ReactElement | null {
  const lines = staleOf(views)
  if (lines.length === 0) return null
  return (
    <div data-testid="stale-line" className={className}>
      {lines.map((line) =>
        line.kind === 'key' ? (
          <p key="key" role="status" className="m-0 text-xs text-muted">
            The server needs a valid API key.
          </p>
        ) : line.kind === 'gone' ? (
          <p key={`gone-${line.what}`} role="status" className="m-0 text-xs text-muted">
            This {line.what} is no longer stored. Showing data from{' '}
            {clock(new Date(line.since).toISOString())} UTC.
          </p>
        ) : (
          <p
            key="stale"
            role="status"
            className="m-0 flex flex-wrap items-center gap-x-2 text-xs text-muted"
          >
            <span>
              Couldn&apos;t refresh. Showing data from {clock(new Date(line.since).toISOString())}{' '}
              UTC.
            </span>
            <RetryButton retry={line.retry} busy={line.retrying} />
          </p>
        ),
      )}
    </div>
  )
}
