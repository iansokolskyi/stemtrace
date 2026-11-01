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

/** Thrown when the API answers with an error status other than 401; `status` tells them apart. */
export class HttpError extends Error {
  readonly status: number
  constructor(message: string, status: number) {
    super(message)
    this.name = 'HttpError'
    this.status = status
  }
}

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
  if (!response.ok) throw new HttpError(errorMessage, response.status)
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
  /** What the attempt printed and logged (terminal events, when captured). */
  output?: string | null
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
  /** Sent by every current server; absent from older ones. */
  task_group?: string | null
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
  reported_parent_id: string | null
  children: string[]
  events: TaskEvent[]
  first_seen: string | null
  last_updated: string | null
  /** Lifetime (first to last event), queue wait included. */
  duration_ms: number | null
  /** Sent by every current server; null when the worker never reported it. */
  exception_type?: string | null
  hostname?: string | null
  queue?: string | null
  task_group?: string | null
  tags?: string[]
  queue_wait_ms?: number | null
  /** The last run's time, queue wait excluded; null until the task finishes. */
  runtime_ms?: number | null
  /** All runs together (retries included); null exactly when `runtime_ms` is. */
  total_runtime_ms?: number | null
  /** Sent by the task detail and list endpoints; null when no event carried it. */
  routing_key?: string | null
  /** When the first attempt was scheduled to run (countdown or eta), ISO 8601. */
  eta?: string | null
}

/** A task as `GET /api/tasks?compact=1` lists it: no events. */
export type ExecutionRow = Omit<TaskNode, 'events'> & { events: null }

export interface ExecutionListResponse {
  tasks: ExecutionRow[]
  total: number
  limit: number
  offset: number
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
  /** Root of the graph that holds the task (what `/api/graphs/{id}` takes); null when unknown. */
  flow_root_id?: string | null
}

/** Totals over a flow's whole graph, on the root nodes of the graphs list. */
export interface FlowSummary {
  /** Tasks in the flow (synthetic group and chord nodes excluded). */
  task_count: number
  /** Tasks per state, by state value; states with no task are left out. */
  state_counts: Record<string, number>
  /** Chords whose callback can't run because a header failed, was rejected, revoked or ignored. */
  not_run_count: number
  /** The worst task state in the flow (the root's own when it has no tasks). */
  state: string
}

export interface GraphNode {
  task_id: string
  name: string
  state: string
  node_type: NodeType
  group_id: string | null
  chord_id: string | null
  parent_id: string | null
  reported_parent_id: string | null
  children: string[]
  duration_ms: number | null
  first_seen: string | null
  last_updated: string | null
  /** The last run's time, queue wait excluded; null until the task finishes. */
  runtime_ms?: number | null
  /** Wait of the first run: from the send (or the ETA, if later) to its first start. */
  queue_wait_ms?: number | null
  /** All runs together (more than `runtime_ms` once a retry ran); null with `runtime_ms`. */
  total_runtime_ms?: number | null
  /** When the first attempt was scheduled to run (a countdown or ETA); null if at once. */
  eta?: string | null
  exception_type?: string | null
  hostname?: string | null
  queue?: string | null
  /** CHORD nodes of a full graph: why the callback has not run (it has no node of its own). */
  not_run_reason?: 'header_failed' | 'header_revoked' | 'header_ignored' | null
  /** Root nodes in the graphs list only; null when the server has no summary for the flow. */
  flow?: FlowSummary | null
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
  /** Kind of the Celery broker (`redis`, `rabbitmq`, `memory`), never its URL; null when unknown. */
  broker?: string | null
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

/**
 * The filters of the Executions page and the timeline, as `GET /api/tasks` takes them. Each is
 * an exact match except `name` (a substring) and `state` (a comma-separated list matches any).
 */
export interface ExecutionFilters {
  /** One state or a comma-separated list (`FAILURE,RETRY`). */
  state?: string
  /** Substring of the task name (the search box). */
  name?: string
  /** The whole task name (a task's "View runs"). */
  name_exact?: string
  /** Exception class of a failure. */
  exception_type?: string
  /** Worker nodename (`celery@host`). */
  hostname?: string
  queue?: string
  task_group?: string
  /** A tag of the task's group. */
  tag?: string
  from_date?: string
  to_date?: string
}

/** The filters plus a relative time window; the API rejects `within` together with `from_date`. */
export interface TimedFilters extends ExecutionFilters {
  /** Updated within this long (`1h`, `6h`); sent instead of `from_date`. */
  within?: string
}

export interface FetchTasksParams extends TimedFilters {
  limit?: number
  offset?: number
}

/** The filters of the Executions list; paging and `compact` are fixed by `fetchExecutions`. */
export type FetchExecutionsParams = TimedFilters

const FILTER_KEYS = [
  'within',
  'state',
  'name',
  'name_exact',
  'exception_type',
  'hostname',
  'queue',
  'task_group',
  'tag',
  'from_date',
  'to_date',
] as const satisfies readonly (keyof TimedFilters)[]

/** Adds each filter that has a value to `searchParams`. */
function setFilterParams(searchParams: URLSearchParams, filters: TimedFilters): void {
  for (const key of FILTER_KEYS) {
    const value = filters[key]
    if (value) searchParams.set(key, value)
  }
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
  if (params) setFilterParams(searchParams, params)

  const url = `${API_BASE}/tasks${searchParams.toString() ? `?${searchParams}` : ''}`
  return apiFetch(url, 'Failed to fetch tasks')
}

/**
 * One page of the Executions list: compact (no events per row), so a page of 200 stays small.
 * `offset` is always sent, 0 included.
 */
export async function fetchExecutions(
  params: FetchExecutionsParams,
  limit: number,
  offset: number,
): Promise<ExecutionListResponse> {
  const searchParams = new URLSearchParams({
    limit: String(limit),
    offset: String(offset),
    compact: '1',
  })
  setFilterParams(searchParams, params)
  return apiFetch(`${API_BASE}/tasks?${searchParams}`, 'Failed to fetch tasks')
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

/** `refresh: false` skips the server's Celery inspect call, which blocks while the broker is slow. */
export interface InspectOptions {
  refresh?: boolean
}

export async function fetchTaskRegistry(
  query?: string,
  status?: TaskStatus,
  { refresh = true }: InspectOptions = {},
): Promise<TaskRegistryResponse> {
  const searchParams = new URLSearchParams()
  if (query) searchParams.set('query', query)
  if (status) searchParams.set('status', status)
  if (!refresh) searchParams.set('refresh', 'false')

  const url = `${API_BASE}/tasks/registry${searchParams.toString() ? `?${searchParams}` : ''}`
  return apiFetch(url, 'Failed to fetch task registry')
}

/**
 * Every worker. By default the server asks Celery first (inspect), so the list is current; the
 * per-host route never does, so no page reads it.
 */
export async function fetchWorkers({
  refresh = true,
}: InspectOptions = {}): Promise<WorkerListResponse> {
  const base = `${API_BASE}/workers`
  return apiFetch(refresh ? base : `${base}?refresh=false`, 'Failed to fetch workers')
}

/** A task group as `GET /api/groups` lists it. */
export interface TaskGroup {
  name: string
  source: 'code' | 'auto'
  tags: string[]
  color: string | null
  task_names: string[]
}

export interface TaskGroupListResponse {
  groups: TaskGroup[]
  total: number
}

export async function fetchGroups(): Promise<TaskGroupListResponse> {
  return apiFetch(`${API_BASE}/groups`, 'Failed to fetch groups')
}

/** `GET /api/stats/timeseries?group_by=task_group`: only the per-group counts are read. */
export interface GroupTimeseriesResponse {
  /** Group name -> state -> runs per bucket; null when the server ignored `group_by`. */
  by_task_group: Record<string, Record<string, number[]>> | null
}

/** Runs per task group and state over `within` (`1h`, `6h`); no `within` is the whole retention. */
export async function fetchGroupTimeseries(within?: string): Promise<GroupTimeseriesResponse> {
  const searchParams = new URLSearchParams()
  if (within) searchParams.set('within', within)
  searchParams.set('group_by', 'task_group')
  // Whole retention: one coarse bucket (the longest the endpoint takes) instead of up to 1440
  if (!within) searchParams.set('bucket', '3650d')
  return apiFetch(`${API_BASE}/stats/timeseries?${searchParams}`, 'Failed to fetch group stats')
}
