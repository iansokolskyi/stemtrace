/**
 * TanStack Query hooks for API data fetching.
 *
 * Polling is only enabled when WebSocket is disconnected.
 * When WS is connected, real-time updates come via WS invalidation.
 */

import { type InfiniteData, useInfiniteQuery, useQuery } from '@tanstack/react-query'
import { useWebSocketContext } from '@/hooks/WebSocketContext'
import {
  type ExecutionListResponse,
  type FetchExecutionsParams,
  type FetchGraphsParams,
  type FetchTasksParams,
  fetchExecutions,
  fetchGraph,
  fetchGraphs,
  fetchGroups,
  fetchGroupTimeseries,
  fetchHealth,
  fetchTask,
  fetchTaskRegistry,
  fetchTasks,
  fetchWorkers,
  type GraphListResponse,
  type GraphResponse,
  type HealthResponse,
  HttpError,
  type TaskDetailResponse,
  type TaskGroupListResponse,
  type TaskListResponse,
  type TaskRegistryResponse,
  type TaskStatus,
  type WorkerListResponse,
} from './client'
import { groupFailureRates } from './groupStats'

// Polling interval when WebSocket is disconnected
const POLL_INTERVAL = 5000
const PAGE_SIZE = 50

export function useTasks(
  params?: Omit<FetchTasksParams, 'limit' | 'offset'>,
  options: { enabled?: boolean } = {},
) {
  const { isConnected } = useWebSocketContext()

  return useQuery<TaskListResponse>({
    enabled: options.enabled,
    queryKey: ['tasks', params],
    queryFn: () => fetchTasks({ ...params, limit: PAGE_SIZE }),
    // Only poll when WS is disconnected
    refetchInterval: isConnected ? false : POLL_INTERVAL,
  })
}

/** Rows per Executions page (the API allows 500). */
export const EXECUTIONS_PAGE_SIZE = 200

/** The Executions list, a page of 200 at a time. A refetch (an event, or the poll while the socket is down) reloads every page loaded so far. */
export function useExecutions(params: FetchExecutionsParams, options: { enabled?: boolean } = {}) {
  const { isConnected } = useWebSocketContext()

  return useInfiniteQuery<
    ExecutionListResponse,
    Error,
    InfiniteData<ExecutionListResponse>,
    unknown[],
    number
  >({
    enabled: options.enabled,
    queryKey: ['executions', params],
    queryFn: ({ pageParam }) => fetchExecutions(params, EXECUTIONS_PAGE_SIZE, pageParam),
    initialPageParam: 0,
    getNextPageParam: (lastPage) => {
      const nextOffset = lastPage.offset + lastPage.limit
      return nextOffset < lastPage.total ? nextOffset : undefined
    },
    // Only poll when WebSocket is disconnected
    refetchInterval: isConnected ? false : POLL_INTERVAL,
  })
}

export function useTask(taskId: string) {
  const { isConnected } = useWebSocketContext()

  return useQuery<TaskDetailResponse>({
    queryKey: ['tasks', taskId],
    queryFn: () => fetchTask(taskId),
    enabled: !!taskId,
    // Only poll when WS is disconnected
    refetchInterval: isConnected ? false : POLL_INTERVAL,
  })
}

export function useGraphs(params?: Omit<FetchGraphsParams, 'limit' | 'offset'>) {
  const { isConnected } = useWebSocketContext()

  return useQuery<GraphListResponse>({
    queryKey: ['graphs', params],
    queryFn: () => fetchGraphs({ ...params, limit: PAGE_SIZE }),
    // Only poll when WS is disconnected
    refetchInterval: isConnected ? false : POLL_INTERVAL,
  })
}

export function useGraphsInfinite(params?: Omit<FetchGraphsParams, 'limit' | 'offset'>) {
  const { isConnected } = useWebSocketContext()

  return useInfiniteQuery<
    GraphListResponse,
    Error,
    InfiniteData<GraphListResponse>,
    unknown[],
    number
  >({
    queryKey: ['graphs-infinite', params],
    queryFn: ({ pageParam }) =>
      fetchGraphs({
        ...params,
        limit: PAGE_SIZE,
        offset: pageParam,
      }),
    initialPageParam: 0,
    getNextPageParam: (lastPage) => {
      const nextOffset = lastPage.offset + lastPage.limit
      return nextOffset < lastPage.total ? nextOffset : undefined
    },
    // Only poll first page when WS is disconnected
    refetchInterval: isConnected ? false : POLL_INTERVAL,
  })
}

export function useGraph(rootId: string) {
  const { isConnected } = useWebSocketContext()

  return useQuery<GraphResponse>({
    queryKey: ['graphs', rootId],
    queryFn: () => fetchGraph(rootId),
    enabled: !!rootId,
    // Only poll when WS is disconnected
    refetchInterval: isConnected ? false : POLL_INTERVAL,
  })
}

export function useHealth() {
  const { isConnected } = useWebSocketContext()

  return useQuery<HealthResponse>({
    queryKey: ['health'],
    queryFn: fetchHealth,
    // Sync with data polling interval so status changes are consistent
    refetchInterval: isConnected ? false : POLL_INTERVAL,
  })
}

export function useTaskRegistry(query?: string, status?: TaskStatus) {
  const { isConnected } = useWebSocketContext()

  return useQuery<TaskRegistryResponse>({
    queryKey: ['taskRegistry', query, status],
    queryFn: () => fetchTaskRegistry(query, status),
    // Registry refreshes less frequently
    refetchInterval: isConnected ? false : 30000,
  })
}

export function useWorkers(hostname?: string) {
  const { isConnected } = useWebSocketContext()

  return useQuery<WorkerListResponse>({
    queryKey: ['workers', hostname],
    queryFn: () => fetchWorkers(hostname),
    // Workers refresh less frequently
    refetchInterval: isConnected ? false : 30000,
  })
}

const GROUPS_POLL_MS = 30_000

/**
 * The task groups, for the sidebar and the filter suggestions. An error (an older server
 * without `/api/groups` answers 404) is not retried: callers treat "no data" as "no groups".
 */
export function useGroups() {
  return useQuery<TaskGroupListResponse>({
    queryKey: ['groups'],
    queryFn: fetchGroups,
    // A 404 means this server has no such endpoint and will not grow one: stop asking. Any other
    // failure may pass, so the next refresh is still due.
    refetchInterval: (query) => {
      const error = query.state.error
      return error instanceof HttpError && error.status === 404 ? false : GROUPS_POLL_MS
    },
    retry: false,
  })
}

/**
 * Each task group's failure rate over `within` (failures / runs, by the run's current state);
 * undefined `within` is the whole retention. No data while loading, and none when the request
 * fails (an older server answers 404): callers then show no rate.
 */
export function useGroupFailureRates(within: string | undefined) {
  return useQuery({
    queryKey: ['stats', 'group-failure-rates', within],
    queryFn: () => fetchGroupTimeseries(within),
    select: groupFailureRates,
    // A 404 means this server has no such endpoint: stop asking. Other failures may pass.
    refetchInterval: (query) => {
      const error = query.state.error
      return error instanceof HttpError && error.status === 404 ? false : GROUPS_POLL_MS
    },
    retry: false,
  })
}
