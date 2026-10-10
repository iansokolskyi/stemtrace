/**
 * API client for stemtrace backend.
 */

// Extend window type for injected base path
declare global {
  interface Window {
    __STEMTRACE_BASE__?: string
    __STEMTRACE_LOGOUT_PATH__?: string
  }
}

// Base URL: use injected base path in production, proxy in dev
export const getBasePath = (): string => window.__STEMTRACE_BASE__ ?? ''
export const getApiBase = (): string => (import.meta.env.DEV ? '/api' : `${getBasePath()}/api`)

// For backwards compatibility (but prefer getApiBase() for lazy evaluation)
export const API_BASE = getApiBase()

/** Thrown when the API responds 401 (session missing or expired). */
export class UnauthorizedError extends Error {
  constructor() {
    super('Authentication required')
    this.name = 'UnauthorizedError'
  }
}

// 401 `reason` sent by the server when the session cookie was signed by a
// different secret (workers/replicas that don't share STEMTRACE_LOGIN_SECRET).
const SESSION_SECRET_MISMATCH = 'session_secret_mismatch'

// Loop guard: if we redirected to login moments ago and still get a 401, don't
// redirect again (the generic error is shown instead).
const LOGIN_REDIRECT_KEY = 'stemtrace:login-redirect-at'
const LOGIN_REDIRECT_COOLDOWN_MS = 10_000

let redirectingToLogin = false
let sessionRejected = false
const sessionRejectedListeners = new Set<() => void>()

export const isRedirectingToLogin = (): boolean => redirectingToLogin

/** True once the server reported a session signed by a different secret. */
export const isSessionRejected = (): boolean => sessionRejected

/** Subscribe to session-rejected changes (shape expected by useSyncExternalStore). */
export function subscribeSessionRejected(listener: () => void): () => void {
  sessionRejectedListeners.add(listener)
  return () => {
    sessionRejectedListeners.delete(listener)
  }
}

function markSessionRejected(): void {
  if (sessionRejected) return
  sessionRejected = true
  for (const listener of sessionRejectedListeners) listener()
}

/**
 * Login page path, or null when built-in form login is not enabled.
 *
 * The server injects the logout path only when form login is on, and the login
 * route lives next to it ("/stemtrace/login" embedded, "/login" for `stemtrace server`).
 */
export const getLoginPath = (): string | null => {
  const logoutPath = window.__STEMTRACE_LOGOUT_PATH__
  if (!logoutPath?.endsWith('/logout')) return null
  return `${logoutPath.slice(0, -'/logout'.length)}/login`
}

/** Login page URL that returns to the current page after signing in. */
export function getLoginUrl(loginPath: string): string {
  const next = `${window.location.pathname}${window.location.search}`
  return `${loginPath}?${new URLSearchParams({ next })}`
}

function redirectToLogin(): void {
  if (redirectingToLogin) return
  const loginPath = getLoginPath()
  if (!loginPath || window.location.pathname === loginPath) return

  const now = Date.now()
  try {
    const lastRedirect = Number(window.sessionStorage.getItem(LOGIN_REDIRECT_KEY) ?? 0)
    if (now - lastRedirect < LOGIN_REDIRECT_COOLDOWN_MS) return
    window.sessionStorage.setItem(LOGIN_REDIRECT_KEY, String(now))
  } catch {
    // Storage unavailable: the module flag still limits this page to one redirect.
  }

  redirectingToLogin = true
  window.location.assign(getLoginUrl(loginPath))
}

async function readUnauthorizedReason(response: Response): Promise<string | null> {
  try {
    const body: unknown = await response.json()
    if (typeof body === 'object' && body !== null && 'reason' in body) {
      return typeof body.reason === 'string' ? body.reason : null
    }
  } catch {
    // Not JSON: no reason.
  }
  return null
}

/**
 * GET a JSON API resource. On 401, explain a session signed by a different
 * secret, otherwise send the user to the login page.
 */
async function apiFetch<T>(url: string, errorMessage: string): Promise<T> {
  const response = await fetch(url)
  if (response.status === 401) {
    if ((await readUnauthorizedReason(response)) === SESSION_SECRET_MISMATCH) {
      // Signing in again wouldn't help, so explain instead of redirecting.
      markSessionRejected()
    } else {
      redirectToLogin()
    }
    throw new UnauthorizedError()
  }
  if (!response.ok) throw new Error(errorMessage)
  return (await response.json()) as T
}

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

export type TaskStatus = 'active' | 'never_run' | 'not_registered'

export interface RegisteredTask {
  name: string
  signature: string | null
  docstring: string | null
  module: string | null
  bound: boolean
  execution_count: number
  registered_by: string[]
  last_run: string | null
  status: TaskStatus
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
  consumer_connected?: boolean | null
  websocket_connections: number
  node_count: number
}

export interface Worker {
  hostname: string
  pid: number
  registered_tasks: string[]
  status: 'online' | 'offline'
  registered_at: string
  last_seen: string
}

export interface WorkerListResponse {
  workers: Worker[]
  total: number
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
  return apiFetch(url, 'Failed to fetch tasks')
}

export async function fetchTask(taskId: string): Promise<TaskDetailResponse> {
  return apiFetch(`${API_BASE}/tasks/${taskId}`, 'Failed to fetch task')
}

export async function fetchGraphs(params?: FetchGraphsParams): Promise<GraphListResponse> {
  const searchParams = new URLSearchParams()
  if (params?.limit) searchParams.set('limit', params.limit.toString())
  if (params?.offset) searchParams.set('offset', params.offset.toString())
  if (params?.from_date) searchParams.set('from_date', params.from_date)
  if (params?.to_date) searchParams.set('to_date', params.to_date)

  const url = `${API_BASE}/graphs${searchParams.toString() ? `?${searchParams}` : ''}`
  return apiFetch(url, 'Failed to fetch graphs')
}

export async function fetchGraph(rootId: string): Promise<GraphResponse> {
  return apiFetch(`${API_BASE}/graphs/${rootId}`, 'Failed to fetch graph')
}

export async function fetchHealth(): Promise<HealthResponse> {
  return apiFetch(`${API_BASE}/health`, 'Failed to fetch health')
}

export async function fetchTaskRegistry(
  query?: string,
  status?: TaskStatus,
): Promise<TaskRegistryResponse> {
  const searchParams = new URLSearchParams()
  if (query) searchParams.set('query', query)
  if (status) searchParams.set('status', status)

  const url = `${API_BASE}/tasks/registry${searchParams.toString() ? `?${searchParams}` : ''}`
  return apiFetch(url, 'Failed to fetch task registry')
}

export async function fetchWorkers(hostname?: string): Promise<WorkerListResponse> {
  const url = hostname
    ? `${API_BASE}/workers/${encodeURIComponent(hostname)}`
    : `${API_BASE}/workers`
  return apiFetch(url, 'Failed to fetch workers')
}
