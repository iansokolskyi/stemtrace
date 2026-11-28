import type { ReactElement } from 'react'
import { getLoginPath, UnauthorizedError } from '@/api/client'
import { reasonOf } from '@/components/Notice'
import { RetryButton } from '@/components/RetryButton'
import type { ViewState } from '@/hooks/useViewState'

/** Whether `error` gets a box at all: an expired session with form login is the redirect's. */
export function showsError(error: unknown): boolean {
  return !(error instanceof UnauthorizedError && getLoginPath() !== null)
}

/**
 * One Overview part's failed first load: what failed, why, and Retry. The box stays mounted
 * while it retries, so it is announced once. A 401 without form login asks for an API key; with
 * it, nothing renders.
 */
export function LoadError({
  what,
  view,
}: {
  /** What failed to load, as a noun phrase: "the runs". */
  what: string
  view: ViewState
}): ReactElement | null {
  if (!showsError(view.error)) return null
  if (view.error instanceof UnauthorizedError) {
    return (
      <p role="alert" className="m-0 px-4 py-3 text-small text-ink">
        The server needs a valid API key.
      </p>
    )
  }
  const reason = reasonOf(view.error)
  return (
    <p
      role="alert"
      className="m-0 flex flex-wrap items-center gap-x-2 px-4 py-3 text-small text-ink"
    >
      <span className="font-medium">Couldn&apos;t load {what}.</span>
      {reason && <span className="text-muted">{reason}</span>}
      <RetryButton retry={view.retry} busy={view.retrying} />
    </p>
  )
}
