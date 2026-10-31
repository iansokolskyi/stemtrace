import { expect, type Page, test, type WebSocketRoute } from '@playwright/test'

import { flowNode, flowNodes } from './fixtures/flow'
import { setupMockApi } from './fixtures/mock-api'
import { createChain, createSuccessTask, type MockTask } from './fixtures/mock-data'

/**
 * Tests for pages that stay current while events arrive faster than the API
 * answers (a busy fleet): the pages refetch one round at a time, so each answer
 * lands and the page keeps changing during the burst, not only after it.
 */

const EVENT_EVERY_MS = 100
const API_LATENCY_MS = 300
const EVENTS = 40

test.describe('Live updates during an event burst', () => {
  test.skip(process.env.E2E_MODE === 'real', 'Mock-only test')

  /** Routes the socket, so a test can send events, and returns the sender. */
  async function liveSocket(page: Page): Promise<() => WebSocketRoute> {
    const sockets: WebSocketRoute[] = []
    await page.routeWebSocket(/\/ws$/, (ws) => {
      sockets.push(ws)
    })
    // StrictMode (dev) opens and drops a socket before the one that goes live
    return () => sockets[sockets.length - 1]
  }

  /** Makes every GET of `path` take `API_LATENCY_MS`, ahead of the mock API. */
  async function slowDown(page: Page, path: RegExp): Promise<void> {
    await page.route(path, async (route) => {
      await new Promise((resolve) => setTimeout(resolve, API_LATENCY_MS))
      await route.fallback()
    })
  }

  /** Sends one event for `task` every `EVENT_EVERY_MS`; `burst.running` says if it is still going. */
  function startBurst(page: Page, socket: WebSocketRoute, task: MockTask) {
    const burst = { running: true, done: Promise.resolve() }
    burst.done = (async () => {
      for (let sent = 0; sent < EVENTS; sent++) {
        socket.send(
          JSON.stringify({
            task_id: task.task_id,
            name: task.name,
            state: task.state,
            timestamp: new Date().toISOString(),
            parent_id: task.parent_id,
            root_id: task.root_id,
            group_id: null,
            trace_id: null,
            retries: 0,
          }),
        )
        await page.waitForTimeout(EVENT_EVERY_MS)
      }
      burst.running = false
    })()
    return burst
  }

  test('the graph shows a new task while the burst is still going', async ({ page }) => {
    const mockApi = await setupMockApi(page, { useDefaults: false })
    const [first, second] = createChain('app.flow', 2)
    mockApi.addTasks([first, second])
    const socket = await liveSocket(page)
    await page.goto(`/flows/${first.task_id}`)
    await expect(page.getByText('Live', { exact: true })).toBeVisible()
    await expect(flowNodes(page)).toHaveCount(2)
    await slowDown(page, /\/api\/graphs\/[^/?]+$/)

    const [, , third] = createChain('app.flow', 3)
    third.task_id = `${first.task_id.slice(0, -4)}ffff`
    third.root_id = first.task_id
    third.parent_id = second.task_id
    second.children.push(third.task_id)
    mockApi.addTask(third)
    const burst = startBurst(page, socket(), third)

    await expect(flowNode(page, third.task_id)).toBeVisible({
      timeout: 3000,
    })
    expect(burst.running).toBe(true)
    await burst.done
  })

  test('the task list shows a new task while the burst is still going', async ({ page }) => {
    const mockApi = await setupMockApi(page, { useDefaults: false })
    const existing = createSuccessTask('app.existing')
    mockApi.addTask(existing)
    const socket = await liveSocket(page)
    await page.goto('/executions')
    await expect(page.getByText('Live', { exact: true })).toBeVisible()
    await expect(page.getByText('app.existing')).toBeVisible()
    await slowDown(page, /\/api\/tasks(\?|$)/)

    const added = createSuccessTask('app.added')
    mockApi.addTask(added)
    const burst = startBurst(page, socket(), added)

    await expect(page.getByText('app.added')).toBeVisible({ timeout: 3000 })
    expect(burst.running).toBe(true)
    await burst.done
  })
})
