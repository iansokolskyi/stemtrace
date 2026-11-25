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

  test('the flow list shows a change made after the last round without another event', async ({
    page,
  }) => {
    await page.clock.install()
    const mockApi = await setupMockApi(page, { useDefaults: false })
    mockApi.addTask(createSuccessTask('app.first'))
    const socket = await liveSocket(page)
    await page.goto('/flows')
    await expect(page.getByText('Live', { exact: true })).toBeVisible()
    await expect(page.getByTestId('flow-row')).toHaveCount(1)

    // The event's round is answered before the server's list has the change (it may serve
    // the flow list up to 1 s stale), so the round still shows one flow.
    const added = createSuccessTask('app.added')
    const round = page.waitForResponse(
      (response) => new URL(response.url()).pathname === '/api/graphs',
    )
    socket().send(
      JSON.stringify({
        task_id: added.task_id,
        name: added.name,
        state: added.state,
        timestamp: new Date().toISOString(),
        parent_id: null,
        root_id: added.root_id,
        group_id: null,
        trace_id: null,
        retries: 0,
      }),
    )
    await round
    await expect(page.getByTestId('flow-row')).toHaveCount(1)

    // The list now has the change, and no further event arrives
    mockApi.addTask(added)
    await page.clock.fastForward(1500)
    await expect(page.getByTestId('flow-row')).toHaveCount(2)
  })

  /** The event for `task`, as the socket delivers it. */
  const eventFor = (task: MockTask): string =>
    JSON.stringify({
      task_id: task.task_id,
      name: task.name,
      state: task.state,
      timestamp: new Date().toISOString(),
      parent_id: null,
      root_id: task.root_id,
      group_id: null,
      trace_id: null,
      retries: 0,
    })

  /** The number of GET /api/graphs list requests so far. */
  function countGraphLists(page: Page): () => number {
    let seen = 0
    page.on('request', (request) => {
      if (new URL(request.url()).pathname === '/api/graphs') seen++
    })
    return () => seen
  }

  test('a burst gets one trailing flow list request, after its last round', async ({ page }) => {
    await page.clock.install()
    const mockApi = await setupMockApi(page, { useDefaults: false })
    mockApi.addTask(createSuccessTask('app.first'))
    const socket = await liveSocket(page)
    await page.goto('/flows')
    await expect(page.getByText('Live', { exact: true })).toBeVisible()
    await expect(page.getByTestId('flow-row')).toHaveCount(1)
    const lists = countGraphLists(page)

    // Events 800 ms apart (under the 1.2 s delay): none of them may be followed by a
    // trailing request while the burst goes on
    for (const name of ['app.a', 'app.b', 'app.c']) {
      const before = lists()
      socket().send(eventFor(createSuccessTask(name)))
      await expect.poll(lists).toBeGreaterThan(before)
      await page.waitForTimeout(300)
      const afterRound = lists()
      await page.clock.fastForward(800)
      await page.waitForTimeout(300)
      expect(lists()).toBe(afterRound)
    }

    // After the last event: exactly one trailing request, and no second one later
    const afterBurst = lists()
    await page.clock.fastForward(1500)
    await expect.poll(lists).toBe(afterBurst + 1)
    await page.clock.fastForward(5000)
    await page.waitForTimeout(300)
    expect(lists()).toBe(afterBurst + 1)
  })

  test('the palette flow list catches up after the last round', async ({ page }) => {
    await page.clock.install()
    const mockApi = await setupMockApi(page, { useDefaults: false })
    mockApi.addTask(createSuccessTask('app.first'))
    const socket = await liveSocket(page)
    await page.goto('/executions')
    await expect(page.getByText('Live', { exact: true })).toBeVisible()
    await page.keyboard.press('Control+k')
    const flows = page.getByRole('dialog', { name: 'Search' }).locator('[data-kind="flow"]')
    await expect(flows).toHaveCount(1)

    // The round is answered before the list has the new flow; no further event arrives
    const added = createSuccessTask('app.added')
    const round = page.waitForResponse(
      (response) => new URL(response.url()).pathname === '/api/graphs',
    )
    socket().send(eventFor(added))
    await round
    await expect(flows).toHaveCount(1)
    mockApi.addTask(added)
    await page.clock.fastForward(1500)
    await expect(flows).toHaveCount(2)
  })
})
