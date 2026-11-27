import type { ReactElement, ReactNode } from 'react'
import { AnomaliesTimeoutError } from '@/api/anomalies'
import { getLoginPath, HttpError, NetworkError, UnauthorizedError } from '@/api/client'
import type { ViewState } from '@/hooks/useViewState'
import { Icon } from './Icon'
import { RetryButton } from './RetryButton'

/** One reason sentence for a failure, or none when the cause is unknown. */
export function reasonOf(error: unknown): string | null {
  if (error instanceof HttpError) return `The server answered ${error.status}.`
  if (error instanceof NetworkError) return 'No response from the server.'
  if (error instanceof AnomaliesTimeoutError) return 'The server took too long to answer.'
  return null
}

interface NoticeProps {
  /** `error` adds the alert icon and announces itself; the default is a plain muted message. */
  tone?: 'error'
  /** Beside the message: Retry, a link back to the list. */
  action?: ReactNode
  children: ReactNode
}

/** The one box for a message that replaces the content: a failed load, or nothing to show. */
export function Notice({ tone, action, children }: NoticeProps): ReactElement {
  if (tone !== 'error') {
    return (
      <div className="rounded-lg bg-surface px-4 py-10 text-center text-small text-muted">
        {children}
        {action && <div className="mt-2">{action}</div>}
      </div>
    )
  }
  return (
    <div
      role="alert"
      data-testid="notice"
      className="flex flex-wrap items-center justify-center gap-x-3 gap-y-2 rounded-lg bg-surface px-4 py-8 text-small"
    >
      <Icon name="alert" size={16} className="flex-none text-failure-t" />
      <span data-testid="notice-message" className="font-semibold text-failure-t">
        {children}
      </span>
      {action}
    </div>
  )
}

/** The end of an infinite list whose next page failed: the loaded pages stay, Retry asks again. */
export function MoreFailed({
  error,
  retry,
  busy,
}: {
  error: unknown
  retry: () => unknown
  busy: boolean
}): ReactElement | null {
  if (error instanceof UnauthorizedError) {
    if (getLoginPath() !== null) return null
    return (
      <p
        role="alert"
        data-testid="more-failed"
        className="m-0 mt-3 text-center text-small text-ink"
      >
        The server needs a valid API key.
      </p>
    )
  }
  const reason = reasonOf(error)
  return (
    <p
      role="alert"
      data-testid="more-failed"
      className="m-0 mt-3 flex flex-wrap items-center justify-center gap-x-2 text-small text-failure-t"
    >
      <span className="font-semibold">Couldn&apos;t load more.{reason && ` ${reason}`}</span>
      <RetryButton retry={retry} busy={busy} />
    </p>
  )
}

interface LoadFailureProps {
  /** What did not load, as it reads after "Couldn't load": "workers", "the flow". */
  what: string
  view: ViewState
  /** A way out beside Retry, such as a link back to the list. */
  action?: ReactNode
}

/**
 * The error Notice for a part with no data: "Couldn't load <what>." and a reason sentence, with
 * Retry. A 401 with form login is the login redirect's, so nothing renders; without it the
 * notice asks for an API key and has no Retry.
 */
export function LoadFailure({ what, view, action }: LoadFailureProps): ReactElement | null {
  if (view.error instanceof UnauthorizedError) {
    if (getLoginPath() !== null) return null
    return <Notice tone="error">The server needs a valid API key.</Notice>
  }
  const reason = reasonOf(view.error)
  return (
    <Notice
      tone="error"
      action={
        <>
          <RetryButton retry={view.retry} busy={view.retrying} />
          {action}
        </>
      }
    >
      {`Couldn't load ${what}.`}
      {reason && ` ${reason}`}
    </Notice>
  )
}
