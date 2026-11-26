import { type ReactElement, useRef, useState } from 'react'
import { getLoginPath, HttpError, NetworkError, UnauthorizedError } from '@/api/client'
import type { ViewState } from '@/hooks/useViewState'

/**
 * A Retry that stays put while it works: the button reads "Retrying…" and ignores clicks (it is
 * `aria-disabled`, not `disabled`, so keyboard focus stays on it). When the retry ends with the
 * button still there (it failed again) focus returns to it; when the button went away with the
 * error, focus moves to the page heading instead of dropping onto the body.
 */
export function RetryButton({
  retry,
  busy = false,
}: {
  retry: () => unknown
  /** A request is already in flight (a poll), so the button shows it too. */
  busy?: boolean
}): ReactElement {
  const [clicked, setClicked] = useState(false)
  const buttonRef = useRef<HTMLButtonElement | null>(null)
  const working = clicked || busy
  const run = async (): Promise<void> => {
    if (working) return
    setClicked(true)
    try {
      await retry()
    } finally {
      setClicked(false)
      requestAnimationFrame(() => {
        if (buttonRef.current?.isConnected) buttonRef.current.focus()
        else if (document.activeElement === document.body) {
          document.querySelector<HTMLElement>('h1')?.focus()
        }
      })
    }
  }
  return (
    <button
      ref={buttonRef}
      type="button"
      aria-disabled={working || undefined}
      onClick={run}
      className="h-6 rounded-[3px] bg-raised px-2.5 text-xs font-semibold hover:bg-press aria-disabled:cursor-progress aria-disabled:text-muted"
    >
      {working ? 'Retrying…' : 'Retry'}
    </button>
  )
}

/** One reason sentence for a failure, or none when the cause is unknown. */
function reasonOf(error: unknown): string | null {
  if (error instanceof HttpError) return `The server answered ${error.status}.`
  if (error instanceof NetworkError) return 'No response from the server.'
  return null
}

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
      <p role="alert" className="m-0 px-4 py-3 text-xs text-ink">
        The server needs a valid API key.
      </p>
    )
  }
  const reason = reasonOf(view.error)
  return (
    <p role="alert" className="m-0 flex flex-wrap items-center gap-x-2 px-4 py-3 text-xs text-ink">
      <span className="font-bold">Couldn&apos;t load {what}.</span>
      {reason && <span className="text-muted">{reason}</span>}
      <RetryButton retry={view.retry} busy={view.retrying} />
    </p>
  )
}
