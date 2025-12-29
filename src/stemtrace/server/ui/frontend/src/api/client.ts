/**
 * API client for stemtrace backend.
 */

// Base URL determined by environment
export const API_BASE = import.meta.env.DEV ? '/api' : '/stemtrace/api'

// Types matching backend schemas
export type NodeType = 'TASK' | 'GROUP' | 'CHORD'

export interface TaskEvent {
  task_id: string
  name: string
  state: string
  timestamp: string
  parent_id: string | null
  root_id: string | null
  group_id: string | null
  trace_id: string | null
  retries: number
  // Enhanced event data
  args: unknown[] | null
  kwargs: Record<string, unknown> | null
  result: unknown | null
  exception: string | null
  traceback: string | null
}

export interface RegisteredTask {
  name: string
  signature: string | null
  docstring: string | null
  module: string | null
  bound: boolean
}

export interface TaskRegistryResponse {
  tasks: RegisteredTask[]
  total: number
}

export interface TaskNode {
  task_id: string
  name: string
  state: string
  node_type: NodeType
  group_id: string | null
  chord_id: string | null
  parent_id: string | null
  children: string[]
  events: TaskEvent[]
  first_seen: string | null
  last_updated: string | null
  duration_ms: number | null
}

export interface TaskListResponse {
  tasks: TaskNode[]
  total: number
  limit: number
  offset: number
}

export interface TaskDetailResponse {
  task: TaskNode
  children: TaskNode[]
}

export interface GraphNode {
  task_id: string
  name: string
  state: string
  node_type: NodeType
  group_id: string | null
  chord_id: string | null
  parent_id: string | null
  children: string[]
  duration_ms: number | null
  first_seen: string | null
  last_updated: string | null
}

export interface GraphResponse {
  root_id: string
  nodes: Record<string, GraphNode>
}

export interface GraphListResponse {
  graphs: GraphNode[]
  total: number
  limit: number
  offset: number
}

export interface HealthResponse {
  status: string
  version: string
  consumer_running: boolean
  websocket_connections: number
  node_count: number
}

export interface FetchTasksParams {
  limit?: number
  offset?: number
  state?: string
  name?: string
  from_date?: string
  to_date?: string
}

export interface FetchGraphsParams {
  limit?: number
  offset?: number
  from_date?: string
  to_date?: string
}

// API functions
export async function fetchTasks(params?: FetchTasksParams): Promise<TaskListResponse> {
  const searchParams = new URLSearchParams()
  if (params?.limit) searchParams.set('limit', params.limit.toString())
  if (params?.offset) searchParams.set('offset', params.offset.toString())
  if (params?.state) searchParams.set('state', params.state)
  if (params?.name) searchParams.set('name', params.name)
  if (params?.from_date) searchParams.set('from_date', params.from_date)
  if (params?.to_date) searchParams.set('to_date', params.to_date)

  const url = `${API_BASE}/tasks${searchParams.toString() ? `?${searchParams}` : ''}`
  const response = await fetch(url)
  if (!response.ok) throw new Error('Failed to fetch tasks')
  return response.json()
}

export async function fetchTask(taskId: string): Promise<TaskDetailResponse> {
  const response = await fetch(`${API_BASE}/tasks/${taskId}`)
  if (!response.ok) throw new Error('Failed to fetch task')
  return response.json()
}

export async function fetchGraphs(params?: FetchGraphsParams): Promise<GraphListResponse> {
  const searchParams = new URLSearchParams()
  if (params?.limit) searchParams.set('limit', params.limit.toString())
  if (params?.offset) searchParams.set('offset', params.offset.toString())
  if (params?.from_date) searchParams.set('from_date', params.from_date)
  if (params?.to_date) searchParams.set('to_date', params.to_date)

  const url = `${API_BASE}/graphs${searchParams.toString() ? `?${searchParams}` : ''}`
  const response = await fetch(url)
  if (!response.ok) throw new Error('Failed to fetch graphs')
  return response.json()
}

export async function fetchGraph(rootId: string): Promise<GraphResponse> {
  const response = await fetch(`${API_BASE}/graphs/${rootId}`)
  if (!response.ok) throw new Error('Failed to fetch graph')
  return response.json()
}

export async function fetchHealth(): Promise<HealthResponse> {
  const response = await fetch(`${API_BASE}/health`)
  if (!response.ok) throw new Error('Failed to fetch health')
  return response.json()
}

export async function fetchTaskRegistry(query?: string): Promise<TaskRegistryResponse> {
  const url = query
    ? `${API_BASE}/tasks/registry?query=${encodeURIComponent(query)}`
    : `${API_BASE}/tasks/registry`
  const response = await fetch(url)
  if (!response.ok) throw new Error('Failed to fetch task registry')
  return response.json()
}
