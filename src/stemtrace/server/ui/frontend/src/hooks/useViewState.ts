import type { UseQueryResult } from '@tanstack/react-query'
import { useRef } from 'react'
import { UnauthorizedError } from '@/api/client'

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
  'data' | 'error' | 'isFetching' | 'errorUpdatedAt' | 'dataUpdatedAt' | 'refetch'
>

/**
 * Decides one part's state from its query, the same way everywhere. The query's own `isError`
 * can't: TanStack resets a query with no data to pending while it refetches, which would flip an
 * error box to "Loading…" on every poll. A failure is "the last error is newer than the last
 * data", which a refetch leaves alone until it succeeds.
 */
export function useViewState(query: ViewQuery): ViewState {
  const lastError = useRef<unknown>(null)
  const hasData = query.data !== undefined
  const failed = query.errorUpdatedAt > query.dataUpdatedAt
  if (query.error !== null) lastError.current = query.error
  else if (!failed) lastError.current = null
  return {
    phase: hasData ? 'data' : failed ? 'error' : 'loading',
    stale: hasData && failed,
    error: lastError.current,
    retrying: query.isFetching,
    updatedAt: query.dataUpdatedAt,
    retry: () => query.refetch(),
  }
}

/**
 * The page's stale line from every view it renders: when the oldest failed part last had data,
 * and a Retry that refetches the failed ones. An expired session (401) is the login redirect's
 * to handle, so it doesn't count.
 */
export function staleOf(
  views: ViewState[],
): { since: number; retrying: boolean; retry: () => Promise<unknown> } | null {
  const failed = views.filter((v) => v.stale && !(v.error instanceof UnauthorizedError))
  if (failed.length === 0) return null
  return {
    since: Math.min(...failed.map((v) => v.updatedAt)),
    retrying: failed.some((v) => v.retrying),
    retry: () => Promise.allSettled(failed.map((v) => v.retry())),
  }
}
