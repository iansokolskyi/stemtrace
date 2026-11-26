import { type ReactElement, type RefObject, useRef, useState } from 'react'
import { HttpError, UnauthorizedError } from '@/api/client'

/** What a part needs to show its first-load failure: the error and the query's `refetch`. */
export interface LoadFailure {
  error: unknown
  retry: () => unknown
}

/**
 * Runs `retry` and reports whether it is in flight. A button the retry removes hands focus to
 * the page heading instead of dropping it on the body; one that stays gets it back.
 */
export function useRetry(retry: () => unknown): {
  pending: boolean
  run: () => Promise<void>
  buttonRef: RefObject<HTMLButtonElement | null>
} {
  const [pending, setPending] = useState(false)
  const buttonRef = useRef<HTMLButtonElement | null>(null)
  const run = async (): Promise<void> => {
    setPending(true)
    try {
      await retry()
    } finally {
      setPending(false)
      // After the render that follows: the button is back, or gone with the error it belonged to
      requestAnimationFrame(() => {
        if (buttonRef.current?.isConnected) buttonRef.current.focus()
        else if (document.activeElement === document.body) {
          document.querySelector<HTMLElement>('h1')?.focus()
        }
      })
    }
  }
  return { pending, run, buttonRef }
}

export function RetryButton({ retry }: { retry: () => unknown }): ReactElement {
  const { pending, run, buttonRef } = useRetry(retry)
  return (
    <button
      ref={buttonRef}
      type="button"
      disabled={pending}
      onClick={run}
      className="h-6 rounded-[3px] bg-raised px-2.5 text-xs font-semibold hover:bg-press disabled:cursor-progress disabled:text-muted"
    >
      {pending ? 'Retrying…' : 'Retry'}
    </button>
  )
}

/** Why a request failed, in words: the status the server answered, or that it did not answer. */
function reasonOf(error: unknown): string {
  return error instanceof HttpError
    ? `The server answered ${error.status}.`
    : 'No response from the server.'
}

/**
 * One Overview part's failed first load: what failed, why, and Retry. An expired session (401)
 * gets no box, since the login redirect handles it.
 */
export function LoadError({
  what,
  failure,
}: {
  /** What failed to load, as a noun phrase: "the runs". */
  what: string
  failure: LoadFailure
}): ReactElement | null {
  if (failure.error instanceof UnauthorizedError) return null
  return (
    <p role="alert" className="m-0 flex flex-wrap items-center gap-x-2 px-4 py-3 text-xs text-ink">
      <span className="font-bold">Couldn&apos;t load {what}.</span>
      <span className="text-muted">{reasonOf(failure.error)}</span>
      <RetryButton retry={failure.retry} />
    </p>
  )
}
