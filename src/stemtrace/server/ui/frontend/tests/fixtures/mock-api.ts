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

import { createSuccessTask, DEFAULT_REGISTRY, DEFAULT_TASKS, type MockTask } from './mock-data'

export interface MockApiContext {
  /** All tasks in the mock store */
  tasks: MockTask[]

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

  const context: MockApiContext = {
    get tasks() {
      return tasks
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
    },

    getTask(taskId: string) {
      return tasks.find((t) => t.task_id === taskId)
    },

    getRootTasks() {
      // Root tasks are those where task_id === root_id
      return tasks.filter((t) => t.task_id === t.root_id)
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
        tasks: DEFAULT_REGISTRY,
        total: DEFAULT_REGISTRY.length,
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

    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        task: serializeTask(task),
        children: [],
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
    const limit = Number.parseInt(url.searchParams.get('limit') || '50', 10)
    const offset = Number.parseInt(url.searchParams.get('offset') || '0', 10)

    let filtered = [...tasks]

    if (state) {
      filtered = filtered.filter((t) => t.state === state)
    }
    if (name) {
      filtered = filtered.filter((t) => t.name.includes(name))
    }

    const paginated = filtered.slice(offset, offset + limit)

    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        tasks: paginated.map(serializeTask),
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
