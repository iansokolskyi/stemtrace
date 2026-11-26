import type { ReactElement } from 'react'
import { HttpError } from '@/api/client'

interface LoadErrorProps {
  /** What failed to load, as a noun phrase: "the runs". */
  what: string
  error: unknown
  /** The query's `refetch`. */
  onRetry: () => unknown
}

/** One Overview part's failed request: what failed, the reason when the server gave one, Retry. */
export function LoadError({ what, error, onRetry }: LoadErrorProps): ReactElement {
  return (
    <p role="alert" className="m-0 flex flex-wrap items-center gap-x-2 px-4 py-3 text-xs text-ink">
      <span className="font-bold">Couldn&apos;t load {what}.</span>
      {error instanceof HttpError && (
        <span className="text-muted">The server answered {error.status}.</span>
      )}
      <button
        type="button"
        onClick={() => onRetry()}
        className="h-6 rounded-[3px] bg-raised px-2.5 font-semibold hover:bg-press"
      >
        Retry
      </button>
    </p>
  )
}
