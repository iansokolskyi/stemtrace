/**
 * Mock API setup for Playwright tests.
 *
 * This intercepts API calls and returns mock data, allowing tests
 * to run without Docker or a real backend.
 *
 * Usage:
 *   import { setupMockApi, MockApiContext } from './fixtures/mock-api'
 *
 *   test('my test', async ({ page }) => {
 *     const mockApi = await setupMockApi(page)
 *     mockApi.addTask(createSuccessTask('my.task', 42))
 *     await page.goto('/')
 *     // ... assertions
 *   })
 */

import type { Page, Route } from '@playwright/test'

import {
  createSuccessTask,
  DEFAULT_REGISTRY,
  DEFAULT_TASKS,
  DEFAULT_WORKERS,
  type MockGroup,
  type MockRegisteredTask,
  type MockTask,
  type MockWorker,
} from './mock-data'

export interface MockApiContext {
  /** All tasks in the mock store */
  tasks: MockTask[]

  /** All workers in the mock store */
  workers: MockWorker[]

  /** Add a task to the mock store */
  addTask: (task: MockTask) => void

  /** Add multiple tasks */
  addTasks: (tasks: MockTask[]) => void

  /** Clear all tasks */
  clearTasks: () => void

  /** Reset to default tasks */
  resetToDefaults: () => void

  /** Get a task by ID */
  getTask: (taskId: string) => MockTask | undefined

  /** Get root tasks (for graphs list) */
  getRootTasks: () => MockTask[]

  /** Replace the task registry (the entries of GET /tasks/registry) */
  setRegistry: (registry: MockRegisteredTask[]) => void

  /** Replace all workers */
  setWorkers: (workers: MockWorker[]) => void

  /** Clear all workers */
  clearWorkers: () => void

  /**
   * Replace the groups GET /groups lists; null makes the endpoint answer 404 (no such route),
   * 'error' a 500 (a server that has the route but fails this time)
   */
  setGroups: (groups: MockGroup[] | null | 'error') => void

  /** Stop intercepting (cleanup) */
  teardown: () => Promise<void>
}

/**
 * Build nodes map for graph response from tasks
 */
function buildNodesMap(tasks: MockTask[], rootId: string): Record<string, unknown> {
  const graphTasks = tasks.filter((t) => t.root_id === rootId)
  const nodes: Record<string, unknown> = {}

  for (const task of graphTasks) {
    nodes[task.task_id] = {
      task_id: task.task_id,
      name: task.name,
      state: task.state,
      parent_id: task.parent_id,
      root_id: task.root_id,
      node_type: task.node_type,
      group_id: task.group_id,
      chord_id: task.chord_id,
      children: task.children,
      events: task.events,
      first_seen: task.first_seen,
      last_updated: task.last_updated,
      duration_ms: task.duration_ms,
    }
  }

  return nodes
}

/**
 * Set up mock API routes on a Playwright page.
 *
 * @param page - Playwright page instance
 * @param options - Configuration options
 * @returns MockApiContext for controlling mock data
 */
export async function setupMockApi(
  page: Page,
  options: {
    /** Start with default mock tasks */
    useDefaults?: boolean
    /** Base URL path for API (default: /api for dev mode, /stemtrace/api for production) */
    apiPath?: string
  } = {},
): Promise<MockApiContext> {
  // In dev mode (Vite), frontend uses /api which gets proxied
  // In production/Docker, it's /stemtrace/api
  // For mock mode, we intercept /api since that's what the frontend calls
  const { useDefaults = true, apiPath = '/api' } = options

  // Mutable task store
  let tasks: MockTask[] = useDefaults ? [...DEFAULT_TASKS] : []
  let workers: MockWorker[] = useDefaults ? [...DEFAULT_WORKERS] : []
  let registry: MockRegisteredTask[] = useDefaults ? [...DEFAULT_REGISTRY] : []
  // No groups by default: the sidebar shows no Groups or Tags section
  let groups: MockGroup[] | null | 'error' = []

  const context: MockApiContext = {
    get tasks() {
      return tasks
    },

    get workers() {
      return workers
    },

    addTask(task: MockTask) {
      tasks.push(task)
    },

    addTasks(newTasks: MockTask[]) {
      tasks.push(...newTasks)
    },

    clearTasks() {
      tasks = []
    },

    resetToDefaults() {
      tasks = [...DEFAULT_TASKS]
      workers = [...DEFAULT_WORKERS]
      registry = [...DEFAULT_REGISTRY]
    },

    getTask(taskId: string) {
      return tasks.find((t) => t.task_id === taskId)
    },

    getRootTasks() {
      // Root tasks are those where task_id === root_id
      return tasks.filter((t) => t.task_id === t.root_id)
    },

    setRegistry(newRegistry: MockRegisteredTask[]) {
      registry = [...newRegistry]
    },

    setWorkers(newWorkers: MockWorker[]) {
      workers = [...newWorkers]
    },

    clearWorkers() {
      workers = []
    },

    setGroups(newGroups: MockGroup[] | null | 'error') {
      groups = newGroups
    },

    async teardown() {
      await page.unrouteAll()
    },
  }

  // === Route handlers ===
  // Note: Routes are matched in order of registration, so register more specific patterns first

  // Helper to serialize task to API format
  const serializeTask = (t: MockTask) => ({
    task_id: t.task_id,
    name: t.name,
    state: t.state,
    parent_id: t.parent_id,
    root_id: t.root_id,
    node_type: t.node_type,
    group_id: t.group_id,
    chord_id: t.chord_id,
    children: t.children,
    events: t.events,
    first_seen: t.first_seen,
    last_updated: t.last_updated,
    duration_ms: t.duration_ms,
    // Optional fields are sent as the real API sends them: always present, null when unknown
    // (Pydantic's None), so "the column has no value anywhere" means null, not a missing key.
    exception_type: t.exception_type ?? null,
    hostname: t.hostname ?? null,
    queue: t.queue ?? null,
    task_group: t.task_group ?? null,
    tags: t.tags ?? [],
    queue_wait_ms: t.queue_wait_ms ?? null,
    runtime_ms: t.runtime_ms ?? null,
    routing_key: t.routing_key ?? null,
    eta: t.eta ?? null,
  })

  // Health endpoint (use regex like other routes)
  await page.route(new RegExp(`${apiPath}/health`), async (route: Route) => {
    if (route.request().method() !== 'GET') {
      await route.continue()
      return
    }

    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        status: 'ok',
        version: '0.1.0',
        consumer_running: true,
        websocket_connections: 0,
        node_count: tasks.length,
      }),
    })
  })

  // Groups: GET /groups (the real API lists them with the task names seen in each)
  await page.route(new RegExp(`${apiPath}/groups(\\?|$)`), async (route: Route) => {
    if (route.request().method() !== 'GET') {
      await route.continue()
      return
    }
    if (groups === 'error') {
      await route.fulfill({
        status: 500,
        contentType: 'application/json',
        body: JSON.stringify({ detail: 'Internal Server Error' }),
      })
      return
    }
    if (groups === null) {
      await route.fulfill({
        status: 404,
        contentType: 'application/json',
        body: JSON.stringify({ detail: 'Not Found' }),
      })
      return
    }
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ groups, total: groups.length }),
    })
  })

  // Registry: GET /tasks/registry (more specific, register first)
  await page.route(new RegExp(`${apiPath}/tasks/registry`), async (route: Route) => {
    if (route.request().method() !== 'GET') {
      await route.continue()
      return
    }

    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        tasks: registry,
        total: registry.length,
      }),
    })
  })

  // Workers list: GET /workers and /workers?...
  await page.route(new RegExp(`${apiPath}/workers(\\?|$)`), async (route: Route) => {
    if (route.request().method() !== 'GET') {
      await route.continue()
      return
    }

    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        workers,
        total: workers.length,
      }),
    })
  })

  // Worker detail: GET /workers/:hostname
  await page.route(new RegExp(`${apiPath}/workers/[^/]+$`), async (route: Route) => {
    if (route.request().method() !== 'GET') {
      await route.continue()
      return
    }

    const url = route.request().url()
    const hostname = decodeURIComponent(url.split('/workers/').pop()?.split('?')[0] || '')
    const filtered = workers.filter((w) => w.hostname === hostname)

    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        workers: filtered,
        total: filtered.length,
      }),
    })
  })

  // Task detail: GET /tasks/:taskId (register before tasks list)
  await page.route(new RegExp(`${apiPath}/tasks/[^/]+$`), async (route: Route) => {
    const url = route.request().url()

    // Skip registry endpoint - use fallback() to pass to next route handler, not continue() which goes to network
    if (url.includes('/tasks/registry')) {
      await route.fallback()
      return
    }

    if (route.request().method() !== 'GET') {
      await route.continue()
      return
    }

    const taskId = url.split('/tasks/').pop()?.split('?')[0]
    const task = tasks.find((t) => t.task_id === taskId)

    if (!task) {
      await route.fulfill({
        status: 404,
        contentType: 'application/json',
        body: JSON.stringify({ detail: 'Task not found' }),
      })
      return
    }

    // Like the real API, list the task's children as nodes
    const children = tasks.filter((t) => task.children.includes(t.task_id))

    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        task: serializeTask(task),
        children: children.map(serializeTask),
      }),
    })
  })

  // Tasks list: GET /tasks (matches /api/tasks and /api/tasks?query=...)
  await page.route(new RegExp(`${apiPath}/tasks(\\?|$)`), async (route: Route) => {
    if (route.request().method() !== 'GET') {
      await route.continue()
      return
    }

    const url = new URL(route.request().url())
    const state = url.searchParams.get('state')
    const name = url.searchParams.get('name')
    const nameExact = url.searchParams.get('name_exact')
    const hostname = url.searchParams.get('hostname')
    const queue = url.searchParams.get('queue')
    const exceptionType = url.searchParams.get('exception_type')
    const taskGroup = url.searchParams.get('task_group')
    const tag = url.searchParams.get('tag')
    const limit = Number.parseInt(url.searchParams.get('limit') || '50', 10)
    const offset = Number.parseInt(url.searchParams.get('offset') || '0', 10)
    // compact=1 leaves out each task's events (`events: null`), as the real API does
    const compact = new Set(['1', 'true']).has(url.searchParams.get('compact') ?? '')

    // The real API lists the most recently updated first (store.list_nodes sorts by
    // last_updated, newest first); a task without one sorts last. Array.sort is stable.
    const updatedAt = (t: MockTask) => (t.last_updated ? Date.parse(t.last_updated) : 0)
    let filtered = [...tasks].sort((a, b) => updatedAt(b) - updatedAt(a))

    if (state) {
      // A comma-separated list matches any of the states, in any case, as the real API does
      const wanted = new Set(state.toUpperCase().split(','))
      filtered = filtered.filter((t) => wanted.has(t.state))
    }
    if (hostname) filtered = filtered.filter((t) => t.hostname === hostname)
    if (queue) filtered = filtered.filter((t) => t.queue === queue)
    if (exceptionType) filtered = filtered.filter((t) => t.exception_type === exceptionType)
    if (taskGroup) filtered = filtered.filter((t) => t.task_group === taskGroup)
    if (tag) filtered = filtered.filter((t) => (t.tags ?? []).includes(tag))
    if (name) {
      filtered = filtered.filter((t) => t.name.toLowerCase().includes(name.toLowerCase()))
    }
    if (nameExact) {
      filtered = filtered.filter((t) => t.name === nameExact)
    }

    const paginated = filtered.slice(offset, offset + limit)

    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        tasks: paginated.map((t) =>
          compact ? { ...serializeTask(t), events: null } : serializeTask(t),
        ),
        total: filtered.length,
        limit,
        offset,
      }),
    })
  })

  // Graph detail: GET /graphs/:rootId (register before graphs list)
  await page.route(new RegExp(`${apiPath}/graphs/[^/]+`), async (route: Route) => {
    if (route.request().method() !== 'GET') {
      await route.continue()
      return
    }

    const url = route.request().url()
    const rootId = url.split('/graphs/').pop()?.split('?')[0]

    if (!rootId) {
      await route.fulfill({
        status: 404,
        contentType: 'application/json',
        body: JSON.stringify({ detail: 'Graph not found' }),
      })
      return
    }

    const nodes = buildNodesMap(tasks, rootId)

    if (Object.keys(nodes).length === 0) {
      await route.fulfill({
        status: 404,
        contentType: 'application/json',
        body: JSON.stringify({ detail: 'Graph not found' }),
      })
      return
    }

    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        root_id: rootId,
        nodes,
      }),
    })
  })

  // Graphs list: GET /graphs (matches /api/graphs and /api/graphs?query=...)
  await page.route(new RegExp(`${apiPath}/graphs(\\?|$)`), async (route: Route) => {
    if (route.request().method() !== 'GET') {
      await route.continue()
      return
    }

    const rootTasks = context.getRootTasks()

    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        graphs: rootTasks.map((t) => ({
          task_id: t.task_id,
          name: t.name,
          state: t.state,
          node_type: t.node_type,
          group_id: t.group_id,
          chord_id: t.chord_id,
          parent_id: t.parent_id,
          children: t.children,
          first_seen: t.first_seen,
          last_updated: t.last_updated,
          duration_ms: t.duration_ms,
        })),
        total: rootTasks.length,
        limit: 50,
        offset: 0,
      }),
    })
  })

  // WebSocket - just acknowledge connection (no real streaming)
  // Playwright doesn't easily mock WebSockets, so we'll skip real WS testing
  // in mock mode

  return context
}

/**
 * Create a pre-configured mock API with specific scenarios
 */
export const mockScenarios = {
  /** Empty state - no tasks */
  empty: async (page: Page) => setupMockApi(page, { useDefaults: false }),

  /** Only simple tasks, no workflows */
  simpleTasks: async (page: Page) => {
    const ctx = await setupMockApi(page, { useDefaults: false })
    ctx.addTask(createSuccessTask('tasks.add', 5))
    ctx.addTask(createSuccessTask('tasks.multiply', 20))
    return ctx
  },

  /** Full default data set */
  defaults: async (page: Page) => setupMockApi(page, { useDefaults: true }),
}
