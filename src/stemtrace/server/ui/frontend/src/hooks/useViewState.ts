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
> & {
  /** An infinite query: the last failure was a next page, which the list says itself. */
  isFetchNextPageError?: boolean
}

/** The last failure per query key, so a remounted error box keeps its reason. */
const REASONS = new Map<string, unknown>()

/**
 * Decides one part's state from its query, the same way everywhere. The query's own `isError`
 * can't: TanStack resets a query with no data to pending while it refetches, which would flip an
 * error box to "Loading…" on every poll. A failure is "the last error is newer than the last
 * data", which a refetch leaves alone until it succeeds. With no data, a failed attempt that is
 * still being retried counts too (`failureCount`), so the box shows from the first failure.
 */
export function useViewState(query: ViewQuery, key?: string): ViewState {
  const lastError = useRef<unknown>(null)
  const hasData = query.data !== undefined
  const failedLast = query.errorUpdatedAt > query.dataUpdatedAt && !query.isFetchNextPageError
  const failed = failedLast || (!hasData && query.failureCount > 0)
  const latest = query.error ?? query.failureReason
  // A remounted box (a route change and back) starts with no error in the ref while the retry
  // runs; `key` (the query's key as a string) lets it find the reason it had
  const remembered = key === undefined ? undefined : REASONS.get(key)
  if (latest !== null) lastError.current = latest
  else if (!failed) lastError.current = null
  else if (lastError.current === null) lastError.current = remembered ?? null
  if (key !== undefined) {
    if (lastError.current !== null) REASONS.set(key, lastError.current)
    else REASONS.delete(key)
  }
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
 * The page's stale lines from every view it renders: when the oldest failed part last had data,
 * with a Retry that refetches the failed ones, and, without form login, one asking for a valid
 * API key when a part's session expired. Both show when parts fail in both ways. With form
 * login a 401 is the login redirect's, so it doesn't count.
 */
export function staleOf(views: ViewState[]): StaleLine[] {
  const held = views.filter((v) => v.stale)
  const lines: StaleLine[] = []
  const unauthorized = held.filter((v) => v.error instanceof UnauthorizedError)
  if (unauthorized.length > 0 && getLoginPath() === null) lines.push({ kind: 'key' })
  const failed = held.filter((v) => !(v.error instanceof UnauthorizedError))
  if (failed.length > 0) {
    lines.push({
      kind: 'stale',
      since: Math.min(...failed.map((v) => v.updatedAt)),
      retrying: failed.some((v) => v.retrying),
      retry: () => Promise.allSettled(failed.map((v) => v.retry())),
    })
  }
  return lines
}
