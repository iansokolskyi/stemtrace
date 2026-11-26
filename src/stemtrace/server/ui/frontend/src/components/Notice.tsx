import type { ReactElement, ReactNode } from 'react'
import { AnomaliesTimeoutError } from '@/api/anomalies'
import { HttpError } from '@/api/client'
import { Icon } from './Icon'

/** Why a request failed, in words a person can act on. */
export function reasonOf(error: unknown): string {
  if (error instanceof HttpError) return `the server answered ${error.status}`
  if (error instanceof AnomaliesTimeoutError) return 'the server took too long to answer'
  // fetch rejects with a TypeError when the server cannot be reached
  if (error instanceof TypeError) return 'the server could not be reached'
  return 'something went wrong'
}

interface NoticeProps {
  /** `error` adds the alert icon and announces itself; the default is a plain muted message. */
  tone?: 'error'
  /** Asks again: the query's `refetch`. Only an error notice has one. */
  onRetry?: () => unknown
  /** A way out beside Retry, such as a link back to the list. */
  action?: ReactNode
  children: ReactNode
}

/** The one box for a message that replaces the content: a failed load, or nothing to show. */
export function Notice({ tone, onRetry, action, children }: NoticeProps): ReactElement {
  if (tone !== 'error') {
    return (
      <div className="rounded-lg bg-surface px-4 py-10 text-center text-xs text-muted">
        {children}
      </div>
    )
  }
  return (
    <div
      role="alert"
      data-testid="notice"
      className="flex flex-wrap items-center justify-center gap-x-3 gap-y-2 rounded-lg bg-surface px-4 py-8 text-xs"
    >
      <Icon name="alert" size={16} className="flex-none text-failure-t" />
      <span data-testid="notice-message" className="font-semibold text-failure-t">
        {children}
      </span>
      {onRetry && (
        <button
          type="button"
          onClick={() => onRetry()}
          className="inline-flex h-[26px] items-center rounded-[3px] bg-raised px-2.5 text-xs font-semibold text-ink hover:bg-press"
        >
          Retry
        </button>
      )}
      {action}
    </div>
  )
}

interface LoadFailureProps {
  /** What did not load, as it reads after "Couldn't load": "workers", "the flow". */
  what: string
  error: unknown
  onRetry: () => unknown
  action?: ReactNode
  /** Said after the reason, e.g. that the last result is still shown. */
  note?: string
}

/** The error Notice for a failed query: "Couldn't load <what>: <reason>." with Retry. */
export function LoadFailure({
  what,
  error,
  onRetry,
  action,
  note,
}: LoadFailureProps): ReactElement {
  const message = `Couldn't load ${what}: ${reasonOf(error)}.`
  return (
    <Notice tone="error" onRetry={onRetry} action={action}>
      {note ? `${message} ${note}` : message}
    </Notice>
  )
}
