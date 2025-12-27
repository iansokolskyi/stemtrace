/**
 * TanStack Query hooks for API data fetching.
 */

import { useQuery } from '@tanstack/react-query'
import {
  fetchGraph,
  fetchGraphs,
  fetchHealth,
  fetchTask,
  fetchTasks,
  type GraphListResponse,
  type GraphResponse,
  type HealthResponse,
  type TaskDetailResponse,
  type TaskListResponse,
} from './client'

export function useTasks(params?: {
  limit?: number
  offset?: number
  state?: string
  name?: string
}) {
  return useQuery<TaskListResponse>({
    queryKey: ['tasks', params],
    queryFn: () => fetchTasks(params),
    refetchInterval: 5000, // Poll every 5 seconds
  })
}

export function useTask(taskId: string) {
  return useQuery<TaskDetailResponse>({
    queryKey: ['tasks', taskId],
    queryFn: () => fetchTask(taskId),
    enabled: !!taskId,
  })
}

export function useGraphs(limit?: number) {
  return useQuery<GraphListResponse>({
    queryKey: ['graphs', limit],
    queryFn: () => fetchGraphs(limit),
    refetchInterval: 5000,
  })
}

export function useGraph(rootId: string) {
  return useQuery<GraphResponse>({
    queryKey: ['graphs', rootId],
    queryFn: () => fetchGraph(rootId),
    enabled: !!rootId,
  })
}

export function useHealth() {
  return useQuery<HealthResponse>({
    queryKey: ['health'],
    queryFn: fetchHealth,
    refetchInterval: 10000,
  })
}
