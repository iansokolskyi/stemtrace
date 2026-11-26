import type { UseQueryResult } from '@tanstack/react-query'
import { useRef } from 'react'
import { getLoginPath, UnauthorizedError } from '@/api/client'

/** What a query-backed part draws: nothing yet, its error box, or its data. */
export type ViewPhase = 'loading' | 'error' | 'data'

export interface ViewState {
  phase: ViewPhase
  /** Data is held but the last attempt failed: keep drawing it, and say so on the page. */
  stale: boolean
  /** The error of the last failed attempt; it outlives the refetch that clears it in the query. */
  error: unknown
  /** A request is in flight: an error box stays and reads "Retrying…", never "Loading…". */
  retrying: boolean
  /** When the data was last fetched successfully (ms); 0 before the first success. */
  updatedAt: number
  retry: () => Promise<unknown>
}

type ViewQuery = Pick<
  UseQueryResult<unknown>,
  | 'data'
  | 'error'
  | 'isFetching'
  | 'errorUpdatedAt'
  | 'dataUpdatedAt'
  | 'failureCount'
  | 'failureReason'
  | 'refetch'
>

/**
 * Decides one part's state from its query, the same way everywhere. The query's own `isError`
 * can't: TanStack resets a query with no data to pending while it refetches, which would flip an
 * error box to "Loading…" on every poll. A failure is "the last error is newer than the last
 * data", which a refetch leaves alone until it succeeds. With no data, a failed attempt that is
 * still being retried counts too (`failureCount`), so the box shows from the first failure.
 */
export function useViewState(query: ViewQuery): ViewState {
  const lastError = useRef<unknown>(null)
  const hasData = query.data !== undefined
  const failedLast = query.errorUpdatedAt > query.dataUpdatedAt
  const failed = failedLast || (!hasData && query.failureCount > 0)
  const latest = query.error ?? query.failureReason
  if (latest !== null) lastError.current = latest
  else if (!failed) lastError.current = null
  return {
    phase: hasData ? 'data' : failed ? 'error' : 'loading',
    stale: hasData && failedLast,
    error: lastError.current,
    retrying: query.isFetching,
    updatedAt: query.dataUpdatedAt,
    retry: () => query.refetch(),
  }
}

export type StaleLine =
  | { kind: 'stale'; since: number; retrying: boolean; retry: () => Promise<unknown> }
  | { kind: 'key' }

/**
 * The page's stale line from every view it renders: when the oldest failed part last had data,
 * and a Retry that refetches the failed ones. An expired session (401) is the login redirect's
 * when form login is on, so it doesn't count; without form login it is one line asking for a
 * valid API key, with no Retry.
 */
export function staleOf(views: ViewState[]): StaleLine | null {
  const held = views.filter((v) => v.stale)
  const unauthorized = held.filter((v) => v.error instanceof UnauthorizedError)
  if (unauthorized.length > 0 && getLoginPath() === null) return { kind: 'key' }
  const failed = held.filter((v) => !(v.error instanceof UnauthorizedError))
  if (failed.length === 0) return null
  return {
    kind: 'stale',
    since: Math.min(...failed.map((v) => v.updatedAt)),
    retrying: failed.some((v) => v.retrying),
    retry: () => Promise.allSettled(failed.map((v) => v.retry())),
  }
}
