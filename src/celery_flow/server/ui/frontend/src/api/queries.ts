/**
 * TanStack Query hooks for API data fetching.
 *
 * Polling is only enabled when WebSocket is disconnected.
 * When WS is connected, real-time updates come via WS invalidation.
 */

import { type InfiniteData, useInfiniteQuery, useQuery } from '@tanstack/react-query'
import { useWebSocketContext } from '@/hooks/WebSocketContext'
import {
  type FetchGraphsParams,
  type FetchTasksParams,
  fetchGraph,
  fetchGraphs,
  fetchHealth,
  fetchTask,
  fetchTaskRegistry,
  fetchTasks,
  type GraphListResponse,
  type GraphResponse,
  type HealthResponse,
  type TaskDetailResponse,
  type TaskListResponse,
  type TaskRegistryResponse,
} from './client'

// Polling interval when WebSocket is disconnected
const POLL_INTERVAL = 5000
const PAGE_SIZE = 50

export function useTasks(params?: Omit<FetchTasksParams, 'limit' | 'offset'>) {
  const { isConnected } = useWebSocketContext()

  return useQuery<TaskListResponse>({
    queryKey: ['tasks', params],
    queryFn: () => fetchTasks({ ...params, limit: PAGE_SIZE }),
    // Only poll when WS is disconnected
    refetchInterval: isConnected ? false : POLL_INTERVAL,
  })
}

export function useTasksInfinite(params?: Omit<FetchTasksParams, 'limit' | 'offset'>) {
  const { isConnected } = useWebSocketContext()

  return useInfiniteQuery<
    TaskListResponse,
    Error,
    InfiniteData<TaskListResponse>,
    unknown[],
    number
  >({
    queryKey: ['tasks-infinite', params],
    queryFn: ({ pageParam }) =>
      fetchTasks({
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

export function useTaskRegistry(query?: string) {
  const { isConnected } = useWebSocketContext()

  return useQuery<TaskRegistryResponse>({
    queryKey: ['taskRegistry', query],
    queryFn: () => fetchTaskRegistry(query),
    // Registry refreshes less frequently
    refetchInterval: isConnected ? false : 30000,
  })
}
