/** TanStack Query hooks for the Overview and its triage panel. */

import { useQuery } from '@tanstack/react-query'
import {
  type ExecutionListResponse,
  type FailureListResponse,
  type FetchExecutionsParams,
  fetchExecutions,
  fetchFailures,
  fetchGroupTimeseries,
  fetchOverview,
  fetchTaskStats,
  fetchTimeseries,
  type GroupTimeseriesResponse,
  type OverviewResponse,
  type TaskStatsResponse,
  type TimeseriesResponse,
} from './client'
import { workersSnapshot } from './queries'

/** `/api/overview` needs a window: All is the longest one it takes (3650 days). */
export const overviewWithin = (range: '1h' | '6h' | 'all'): string =>
  range === 'all' ? '3650d' : range

/** The other summary endpoints start at the oldest run held when no `within` is sent (All). */
export const statsWithin = (range: '1h' | '6h' | 'all'): string | undefined =>
  range === 'all' ? undefined : range

// The Overview's numbers are aggregates, which events do not invalidate: they refresh on this
// interval with the socket up or down.
const OVERVIEW_POLL_MS = 15_000

/** The KPIs for the window and the window before it. */
export function useOverview(within: string) {
  return useQuery<OverviewResponse>({
    queryKey: ['overview', 'kpis', within],
    queryFn: () => fetchOverview(within),
    refetchInterval: OVERVIEW_POLL_MS,
  })
}

/** The bucket the Runs chart draws, as the prototype does. All lets the server choose one. */
export const CHART_BUCKET = '5m'

/** Runs per bucket by state, behind the chart and the tiles' sparklines (one request). */
export function useRunsSeries(within: string | undefined, bucket: string | undefined) {
  return useQuery<TimeseriesResponse>({
    queryKey: ['overview', 'series', within, bucket],
    queryFn: () => fetchTimeseries(within, bucket),
    refetchInterval: OVERVIEW_POLL_MS,
    // A span list waits for this answer, so a failure must show at once, not after retries
    retry: false,
  })
}

/** Failures over the window, grouped by exception class. */
export function useFailures(within: string | undefined) {
  return useQuery<FailureListResponse>({
    queryKey: ['overview', 'failures', within],
    queryFn: () => fetchFailures(within),
    refetchInterval: OVERVIEW_POLL_MS,
  })
}

/** Per-name stats and per-group rollups over the window. */
export function useWindowTaskStats(within: string | undefined) {
  return useQuery<TaskStatsResponse>({
    queryKey: ['overview', 'task-stats', within],
    queryFn: () => fetchTaskStats(within),
    refetchInterval: OVERVIEW_POLL_MS,
  })
}

/** Runs per task group and state over the window (the health table's sparklines). */
export function useGroupSeries(within: string | undefined, enabled: boolean) {
  return useQuery<GroupTimeseriesResponse>({
    enabled,
    queryKey: ['overview', 'group-series', within],
    queryFn: () => fetchGroupTimeseries(within),
    refetchInterval: OVERVIEW_POLL_MS,
    retry: false,
  })
}

/** What the server knows of the workers (no Celery inspect), refreshed for the strip. */
export function useWorkersStrip() {
  return useQuery({ ...workersSnapshot, refetchInterval: OVERVIEW_POLL_MS })
}

/** The first `limit` rows of the filtered, compact run list behind a panel entry. */
export function usePanelRuns(
  params: FetchExecutionsParams,
  limit: number,
  options: { enabled?: boolean } = {},
) {
  return useQuery<ExecutionListResponse>({
    enabled: options.enabled,
    queryKey: ['executions', 'panel', params, limit],
    queryFn: () => fetchExecutions(params, limit, 0),
    // Showing more keeps the rows already on screen while the longer list loads
    placeholderData: (previous) => previous,
    refetchInterval: OVERVIEW_POLL_MS,
  })
}
