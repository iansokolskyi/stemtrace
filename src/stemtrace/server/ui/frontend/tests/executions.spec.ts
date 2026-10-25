import { expect, type Page, test, type WebSocketRoute } from '@playwright/test'

import { setupMockApi } from './fixtures/mock-api'
import { createSuccessTask, type MockTask } from './fixtures/mock-data'

/**
 * The Executions page: the virtualised table, its requests, density and live updates.
 * Mock mode only: it controls the API, the viewport and the WebSocket.
 */

test.skip(process.env.E2E_MODE === 'real', 'Mock-only test')

/** `count` successful tasks, the first the most recently updated (as the API lists them). */
function manyTasks(count: number, make: (index: number) => Partial<MockTask> = () => ({})) {
  const base = Date.parse('2026-10-17T12:00:00Z')
  return Array.from({ length: count }, (_, index) => {
    const task = createSuccessTask(`app.jobs.task_${String(index).padStart(4, '0')}`)
    task.last_updated = new Date(base - index * 1000).toISOString()
    return Object.assign(task, make(index))
  })
}

/** Records the query of every GET /api/tasks list request, in order. */
function recordTaskLists(page: Page): Record<string, string>[] {
  const seen: Record<string, string>[] = []
  page.on('request', (request) => {
    const url = new URL(request.url())
    if (url.pathname === '/api/tasks') seen.push(Object.fromEntries(url.searchParams))
  })
  return seen
}

test.describe('Executions requests', () => {
  test('the list asks for exactly the filters in the URL, 200 rows, compact', async ({ page }) => {
    const api = await setupMockApi(page, { useDefaults: false })
    api.addTasks(manyTasks(450))
    const lists = recordTaskLists(page)

    await page.goto(
      '/executions?state=SUCCESS&name=jobs&name_exact=app.jobs.task_0001&from_date=2026-10-01&to_date=2026-10-02',
    )
    await expect(page.getByText('app.jobs.task_0001', { exact: true })).toBeVisible()

    const filters = {
      state: 'SUCCESS',
      name: 'jobs',
      name_exact: 'app.jobs.task_0001',
      from_date: '2026-10-01',
      to_date: '2026-10-02',
    }
    // The full parameter set, not a substring: nothing extra, nothing missing
    expect(lists[0]).toEqual({ limit: '200', offset: '0', compact: '1', ...filters })
    // Nothing is scrolled, so the next page is not asked for yet (the table box has a height
    // from the first paint; an unbounded box would render every row and load page after page)
    await page.waitForTimeout(500)
    expect(lists).toHaveLength(1)
  })

  test('the next page asks for the next offset with the same parameters', async ({ page }) => {
    const api = await setupMockApi(page, { useDefaults: false })
    api.addTasks(manyTasks(450))
    const lists = recordTaskLists(page)

    await page.goto('/executions?state=SUCCESS')
    await expect(page.getByText('app.jobs.task_0000', { exact: true })).toBeVisible()
    await scrollToBottom(page)

    await expect.poll(() => lists.some((q) => q.offset === '200')).toBe(true)
    const second = lists.find((q) => q.offset === '200')
    expect(second).toEqual({ limit: '200', offset: '200', compact: '1', state: 'SUCCESS' })
  })

  test('without filters the request has only the paging parameters', async ({ page }) => {
    const api = await setupMockApi(page, { useDefaults: false })
    api.addTasks(manyTasks(3))
    const lists = recordTaskLists(page)

    await page.goto('/executions')
    await expect(page.getByText('app.jobs.task_0000', { exact: true })).toBeVisible()

    expect(lists[0]).toEqual({ limit: '200', offset: '0', compact: '1' })
  })
})

const rows = (page: Page) => page.locator('a[href^="/tasks/"]')
const scrollBox = (page: Page) => page.getByTestId('executions-scroll')
const header = (page: Page, column: string) => page.locator(`[data-column-header="${column}"]`)

/** Scrolls the table box to the bottom, as a user dragging the scrollbar would. */
async function scrollToBottom(page: Page): Promise<void> {
  await scrollBox(page).evaluate((el) => {
    el.scrollTop = el.scrollHeight
  })
}

test.describe('Executions table', () => {
  test('renders few rows for 2,000 tasks and reaches the last one by scrolling', async ({
    page,
  }) => {
    const api = await setupMockApi(page, { useDefaults: false })
    api.addTasks(manyTasks(2000))
    await page.goto('/executions')
    await expect(page.getByText('app.jobs.task_0000', { exact: true })).toBeVisible()

    expect(await rows(page).count()).toBeLessThan(60)
    expect(await rows(page).count()).toBeGreaterThan(5)

    // Each scroll to the end loads the next page of 200 until the last task is there
    await expect
      .poll(
        async () => {
          await scrollToBottom(page)
          return page.getByText('app.jobs.task_1999', { exact: true }).isVisible()
        },
        { timeout: 20000 },
      )
      .toBe(true)
    expect(await rows(page).count()).toBeLessThan(60)
  })

  test('each row shows state, name, group, worker, queue, waits, runtime and age', async ({
    page,
  }) => {
    const api = await setupMockApi(page, { useDefaults: false })
    const [task] = manyTasks(1, () => ({
      task_group: 'Billing',
      hostname: 'celery@worker-7',
      queue: 'invoices',
      queue_wait_ms: 120,
      runtime_ms: 1500,
      last_updated: new Date(Date.now() - 5 * 60 * 1000).toISOString(),
    }))
    api.addTask(task)
    await page.goto('/executions')

    const row = rows(page).first()
    await expect(row).toHaveAttribute('href', `/tasks/${task.task_id}`)
    await expect(row.locator('[data-state="SUCCESS"]')).toHaveText('Success')
    await expect(row.getByText('app.jobs.task_0000', { exact: true })).toBeVisible()
    await expect(row.locator('[data-cell="group"]')).toHaveText('Billing')
    // The worker shows without the `celery@` every hostname starts with; hover has it whole
    await expect(row.locator('[data-cell="worker"]')).toHaveText('worker-7')
    await expect(row.locator('[data-cell="worker"]')).toHaveAttribute('title', 'celery@worker-7')
    await expect(row.locator('[data-cell="queue"]')).toHaveText('invoices')
    await expect(row.locator('[data-cell="queue_wait"]')).toHaveText('120ms')
    await expect(row.locator('[data-cell="runtime"]')).toHaveText('1.5s')
    await expect(row.locator('[data-cell="updated"]')).toHaveText('5m ago')
    await row.click()
    await expect(page).toHaveURL(new RegExp(`/tasks/${task.task_id}$`))
  })

  const OPTIONAL: { column: string; field: Partial<MockTask>; label: string }[] = [
    { column: 'group', field: { task_group: 'Billing' }, label: 'Group' },
    { column: 'worker', field: { hostname: 'celery@w1' }, label: 'Worker' },
    { column: 'queue', field: { queue: 'default' }, label: 'Queue' },
    { column: 'queue_wait', field: { queue_wait_ms: 40 }, label: 'Queue wait' },
    { column: 'runtime', field: { runtime_ms: 900 }, label: 'Runtime' },
  ]
  for (const { column, field, label } of OPTIONAL) {
    test(`the ${label} column is hidden when no row has a value and shown when one does`, async ({
      page,
    }) => {
      const api = await setupMockApi(page, { useDefaults: false })
      // Every field is present as the API sends it, null: the column has nothing to show
      api.addTasks(manyTasks(5))
      await page.goto('/executions')
      await expect(page.getByText('app.jobs.task_0000', { exact: true })).toBeVisible()
      await expect(header(page, 'name')).toBeVisible()
      await expect(header(page, column)).toHaveCount(0)

      // One row with a value is enough
      api.clearTasks()
      api.addTasks(manyTasks(5, (index) => (index === 3 ? field : {})))
      await page.goto('/executions')
      await expect(header(page, column)).toHaveText(label)
    })
  }

  test('the column set is decided by the first page and never changes while scrolling', async ({
    page,
  }) => {
    const api = await setupMockApi(page, { useDefaults: false })
    // Page 1 (rows 0-199) has a worker and no queue; page 2 (200-399) has a queue and no worker
    api.addTasks(
      manyTasks(400, (index) =>
        index < 200 ? { hostname: 'celery@w1' } : { queue: 'late', hostname: null },
      ),
    )
    await page.goto('/executions')
    await expect(header(page, 'worker')).toHaveText('Worker')
    await expect(header(page, 'queue')).toHaveCount(0)

    await expect
      .poll(
        async () => {
          await scrollToBottom(page)
          return page.getByText('app.jobs.task_0399', { exact: true }).isVisible()
        },
        { timeout: 15000 },
      )
      .toBe(true)
    // Page 2 has arrived: its queue did not add a column, its missing worker did not drop one
    await expect(header(page, 'queue')).toHaveCount(0)
    await expect(header(page, 'worker')).toHaveText('Worker')
  })

  test('at 1280px every column fits without the box scrolling sideways', async ({ page }) => {
    const api = await setupMockApi(page, { useDefaults: false })
    api.addTasks(
      manyTasks(5, () => ({
        task_group: 'Billing',
        hostname: 'celery@worker-billing-1',
        queue: 'invoices',
        queue_wait_ms: 40,
        runtime_ms: 900,
      })),
    )
    await page.goto('/executions')
    for (const column of ['group', 'worker', 'queue', 'queue_wait', 'runtime', 'updated']) {
      await expect(header(page, column)).toBeVisible()
    }
    expect(await scrollBox(page).evaluate((el) => el.scrollWidth - el.clientWidth)).toBe(0)
    // The longest worker name is not cut
    const worker = rows(page).first().locator('[data-cell="worker"]')
    expect(await worker.evaluate((el) => el.scrollWidth <= el.clientWidth)).toBe(true)
  })

  test('at 400px the page does not scroll sideways but the table box does', async ({ page }) => {
    await page.setViewportSize({ width: 400, height: 800 })
    const api = await setupMockApi(page, { useDefaults: false })
    api.addTasks(
      manyTasks(30, () => ({
        task_group: 'Billing',
        hostname: 'celery@w1',
        queue: 'default',
        queue_wait_ms: 40,
        runtime_ms: 900,
      })),
    )
    await page.goto('/executions')
    await expect(page.getByText('app.jobs.task_0000', { exact: true })).toBeVisible()

    const overflow = () =>
      page.evaluate(
        () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
      )
    expect(await overflow()).toBeLessThanOrEqual(0)

    const box = scrollBox(page)
    expect(await box.evaluate((el) => el.scrollWidth - el.clientWidth)).toBeGreaterThan(100)
    const rect = await box.boundingBox()
    expect(rect).not.toBeNull()
    await page.mouse.move((rect?.x ?? 0) + 200, (rect?.y ?? 0) + 100)
    await page.mouse.wheel(300, 0)
    await expect.poll(() => box.evaluate((el) => el.scrollLeft)).toBeGreaterThan(100)
    expect(await overflow()).toBeLessThanOrEqual(0)
  })
})

test.describe('Density toggle', () => {
  const density = (page: Page, name: 'Compact' | 'Comfortable') =>
    page.getByRole('group', { name: 'Row density' }).getByRole('button', { name })
  const rowHeight = async (page: Page) => (await rows(page).first().boundingBox())?.height

  test('rows are comfortable by default and compact on request', async ({ page }) => {
    const api = await setupMockApi(page, { useDefaults: false })
    api.addTasks(manyTasks(5))
    await page.goto('/executions')
    await expect(rows(page)).toHaveCount(5)
    await expect(density(page, 'Comfortable')).toHaveAttribute('aria-pressed', 'true')
    await expect(density(page, 'Compact')).toHaveAttribute('aria-pressed', 'false')
    expect(await rowHeight(page)).toBe(44)

    await density(page, 'Compact').click()
    await expect(density(page, 'Compact')).toHaveAttribute('aria-pressed', 'true')
    expect(await rowHeight(page)).toBe(32)
    // The rows stay under one another: the second starts where the first ends
    const [first, second] = await Promise.all([
      rows(page).nth(0).boundingBox(),
      rows(page).nth(1).boundingBox(),
    ])
    expect((second?.y ?? 0) - (first?.y ?? 0)).toBe(32)
  })

  test('the choice is kept per viewer in storage, not in the URL, and survives a reload', async ({
    page,
  }) => {
    const api = await setupMockApi(page, { useDefaults: false })
    api.addTasks(manyTasks(5))
    await page.goto('/executions')
    await density(page, 'Compact').click()
    expect(await page.evaluate(() => localStorage.getItem('stemtrace-density'))).toBe('compact')
    expect(new URL(page.url()).searchParams.has('density')).toBe(false)

    await page.reload()
    await expect(density(page, 'Compact')).toHaveAttribute('aria-pressed', 'true')
    expect(await rowHeight(page)).toBe(32)
  })

  test('a stored value that is not a density falls back to comfortable', async ({ page }) => {
    const api = await setupMockApi(page, { useDefaults: false })
    api.addTasks(manyTasks(5))
    await page.addInitScript(() => localStorage.setItem('stemtrace-density', 'huge'))
    await page.goto('/executions')
    await expect(density(page, 'Comfortable')).toHaveAttribute('aria-pressed', 'true')
    expect(await rowHeight(page)).toBe(44)
  })

  test('without usable storage the page works and the toggle still switches', async ({ page }) => {
    const api = await setupMockApi(page, { useDefaults: false })
    api.addTasks(manyTasks(5))
    // Private windows and blocked site data make every access throw
    await page.addInitScript(() => {
      Object.defineProperty(window, 'localStorage', {
        get() {
          throw new DOMException('denied', 'SecurityError')
        },
      })
    })
    await page.goto('/executions')
    await expect(rows(page)).toHaveCount(5)
    expect(await rowHeight(page)).toBe(44)
    await density(page, 'Compact').click()
    expect(await rowHeight(page)).toBe(32)
  })
})

test.describe('Live updates under the cursor', () => {
  /** Routes the socket so a test can send events; StrictMode (dev) drops the first socket. */
  async function liveSocket(page: Page): Promise<() => WebSocketRoute> {
    const sockets: WebSocketRoute[] = []
    await page.routeWebSocket(/\/ws$/, (ws) => {
      sockets.push(ws)
    })
    return () => sockets[sockets.length - 1]
  }

  /** The event the server sends when `task` changes. */
  function send(socket: WebSocketRoute, task: MockTask): void {
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
  }

  /** A task updated after every task `manyTasks` makes, so the API lists it first. */
  function newest(name: string): MockTask {
    const task = createSuccessTask(name)
    task.last_updated = '2026-10-17T13:00:00.000Z'
    return task
  }

  const newButton = (page: Page, count: number) =>
    page.getByRole('button', { name: `${count} new` })
  const names = (page: Page) => rows(page).locator('[data-task-last]')

  test('under the pointer the order is frozen, rows update in place, new tasks are counted', async ({
    page,
  }) => {
    const api = await setupMockApi(page, { useDefaults: false })
    const tasks = manyTasks(6)
    api.addTasks(tasks)
    const socket = await liveSocket(page)
    await page.goto('/executions')
    await expect(page.getByText('Live', { exact: true })).toBeVisible()
    await expect(rows(page)).toHaveCount(6)

    const hovered = rows(page).nth(2)
    await hovered.hover()
    const before = await hovered.boundingBox()
    const boxBefore = await scrollBox(page).boundingBox()
    await expect(hovered).toContainText('task_0002')

    // Something a visible row shows changes, and a task arrives that sorts above all of them
    tasks[4].state = 'FAILURE'
    const added = newest('app.jobs.brand_new')
    api.addTask(added)
    send(socket(), added)

    await expect(newButton(page, 1)).toBeVisible()
    // The failed row updated where it is; the new task is not in the table yet
    await expect(rows(page).nth(4).locator('[data-state="FAILURE"]')).toBeVisible()
    await expect(rows(page)).toHaveCount(6)
    await expect(names(page).first()).toHaveText('task_0000')
    // The hovered row did not move or change, and the table box did not shift
    await expect(hovered).toContainText('task_0002')
    expect(await hovered.boundingBox()).toEqual(before)
    expect(await scrollBox(page).boundingBox()).toEqual(boxBefore)

    await newButton(page, 1).click()
    await expect(names(page).first()).toHaveText('brand_new')
    await expect(rows(page)).toHaveCount(7)
    await expect(newButton(page, 1)).toHaveCount(0)
  })

  test('with the pointer outside and the table at its top, new tasks appear first', async ({
    page,
  }) => {
    const api = await setupMockApi(page, { useDefaults: false })
    api.addTasks(manyTasks(6))
    const socket = await liveSocket(page)
    await page.goto('/executions')
    await expect(page.getByText('Live', { exact: true })).toBeVisible()
    await expect(rows(page)).toHaveCount(6)

    const added = newest('app.jobs.brand_new')
    api.addTask(added)
    send(socket(), added)

    await expect(names(page).first()).toHaveText('brand_new')
    await expect(rows(page)).toHaveCount(7)
    await expect(page.getByRole('button', { name: /new$/ })).toHaveCount(0)
  })

  test('scrolled away from the top the order is frozen until it is back at the top', async ({
    page,
  }) => {
    const api = await setupMockApi(page, { useDefaults: false })
    api.addTasks(manyTasks(60))
    const socket = await liveSocket(page)
    await page.goto('/executions')
    await expect(page.getByText('Live', { exact: true })).toBeVisible()
    await expect(rows(page).first()).toBeVisible()

    await scrollBox(page).evaluate((el) => {
      el.scrollTop = 600
    })
    const added = newest('app.jobs.brand_new')
    api.addTask(added)
    send(socket(), added)
    await expect(newButton(page, 1)).toBeVisible()
    // Still frozen a moment later: nothing was inserted above what is on screen
    expect(await scrollBox(page).evaluate((el) => el.scrollTop)).toBe(600)

    // Back at the top, with the pointer outside the table, the new order applies
    await scrollBox(page).evaluate((el) => {
      el.scrollTop = 0
    })
    await expect(newButton(page, 1)).toHaveCount(0)
    await expect(names(page).first()).toHaveText('brand_new')
  })

  test('older pages loaded while frozen are appended, not counted as new', async ({ page }) => {
    const api = await setupMockApi(page, { useDefaults: false })
    api.addTasks(manyTasks(450))
    await page.goto('/executions')
    await expect(rows(page).first()).toBeVisible()

    // Scrolling down freezes the order; the next pages must still arrive at the end
    await expect
      .poll(
        async () => {
          await scrollToBottom(page)
          return page.getByText('task_0449', { exact: true }).isVisible()
        },
        { timeout: 20000 },
      )
      .toBe(true)
    await expect(page.getByRole('button', { name: /new$/ })).toHaveCount(0)
    await expect(page.getByText(/Showing 450 of 450 tasks/)).toBeVisible()
  })

  // The server re-sorts by last_updated on every refetch, so an event moves a task to the top and
  // a new task pushes the bottom row out of a page. These tests model that: they bump
  // last_updated, and use the three positions of the changed row and one or several new tasks.

  /** A task newer than every `manyTasks` row, `n` seconds after the first new one. */
  function arrival(name: string, n: number, fields: Partial<MockTask> = {}): MockTask {
    const task = createSuccessTask(name)
    task.last_updated = new Date(Date.parse('2026-10-17T13:00:00Z') + n * 1000).toISOString()
    return Object.assign(task, fields)
  }

  for (const position of [0, 2, 5]) {
    for (const arrivals of [1, 3]) {
      test(`a shown task at row ${position} that moves to the top is not mistaken for the anchor, ${arrivals} new`, async ({
        page,
      }) => {
        const api = await setupMockApi(page, { useDefaults: false })
        const tasks = manyTasks(6)
        api.addTasks(tasks)
        const socket = await liveSocket(page)
        await page.goto('/executions')
        await expect(page.getByText('Live', { exact: true })).toBeVisible()
        await expect(rows(page)).toHaveCount(6)
        const before = await names(page).allTextContents()

        await rows(page).nth(2).hover()
        // The task at `position` gets an event: it is now the most recently updated of all
        tasks[position].state = 'FAILURE'
        tasks[position].last_updated = '2026-10-17T14:00:00.000Z'
        const added = Array.from({ length: arrivals }, (_, i) => arrival(`app.jobs.fresh_${i}`, i))
        api.addTasks(added)
        send(socket(), added[0])

        await expect(newButton(page, arrivals)).toBeVisible()
        // The same six rows in the same order; the bumped one shows its new state in place
        await expect(rows(page)).toHaveCount(6)
        await expect(names(page)).toHaveText(before)
        await expect(rows(page).nth(position).locator('[data-state="FAILURE"]')).toBeVisible()
        await expect(page.getByText(/Showing 6 of /)).toBeVisible()
      })
    }
  }

  for (const arrivals of [1, 3]) {
    test(`with 250 tasks and only the first page loaded, ${arrivals} new push the last row out of the page and are counted`, async ({
      page,
    }) => {
      const api = await setupMockApi(page, { useDefaults: false })
      api.addTasks(manyTasks(250))
      const socket = await liveSocket(page)
      await page.goto('/executions')
      await expect(page.getByText('Live', { exact: true })).toBeVisible()
      await expect(page.getByText(/Showing 200 of 250 tasks/)).toBeVisible()

      await rows(page).first().hover()
      const added = Array.from({ length: arrivals }, (_, i) => arrival(`app.jobs.fresh_${i}`, i))
      api.addTasks(added)
      send(socket(), added[0])

      await expect(newButton(page, arrivals)).toBeVisible()
      // Nothing was appended at the bottom as an "older page"; the first row is still the first
      await expect(names(page).first()).toHaveText('task_0000')
      await expect(page.getByText(new RegExp(`of ${250 + arrivals} tasks`))).toBeVisible()
      await expect(page.getByText('fresh_0', { exact: true })).toHaveCount(0)

      await newButton(page, arrivals).click()
      await expect(names(page).first()).toHaveText(`fresh_${arrivals - 1}`)
    })
  }

  test('with 250 tasks and the second page loaded, a new task is counted and the rows stay', async ({
    page,
  }) => {
    const api = await setupMockApi(page, { useDefaults: false })
    api.addTasks(manyTasks(250))
    const socket = await liveSocket(page)
    await page.goto('/executions')
    await expect(page.getByText('Live', { exact: true })).toBeVisible()
    await expect
      .poll(
        async () => {
          await scrollToBottom(page)
          return page.getByText('task_0249', { exact: true }).isVisible()
        },
        { timeout: 20000 },
      )
      .toBe(true)
    await expect(page.getByText(/Showing 250 of 250 tasks/)).toBeVisible()

    const added = arrival('app.jobs.fresh_0', 0)
    api.addTask(added)
    send(socket(), added)

    await expect(newButton(page, 1)).toBeVisible()
    await expect(page.getByText(/Showing 250 of 251 tasks/)).toBeVisible()
    await expect(page.getByText('task_0249', { exact: true })).toBeVisible()
  })

  for (const position of [0, 2, 5]) {
    for (const arrivals of [1, 3]) {
      test(`in a filtered list, the task at row ${position} finishing leaves the list and ${arrivals} new are counted`, async ({
        page,
      }) => {
        const api = await setupMockApi(page, { useDefaults: false })
        const tasks = manyTasks(6, () => ({ state: 'STARTED' }))
        api.addTasks(tasks)
        const socket = await liveSocket(page)
        await page.goto('/executions?state=STARTED')
        await expect(page.getByText('Live', { exact: true })).toBeVisible()
        await expect(rows(page)).toHaveCount(6)
        const before = await names(page).allTextContents()

        await rows(page).nth(2).hover()
        // It finishes: no longer STARTED, so the filtered list drops it
        tasks[position].state = 'SUCCESS'
        tasks[position].last_updated = '2026-10-17T14:00:00.000Z'
        const added = Array.from({ length: arrivals }, (_, i) =>
          arrival(`app.jobs.fresh_${i}`, i, { state: 'STARTED' }),
        )
        api.addTasks(added)
        send(socket(), added[0])

        await expect(newButton(page, arrivals)).toBeVisible()
        // The finished task is gone, not left showing "Running"; the others keep their order
        await expect(rows(page)).toHaveCount(5)
        await expect(names(page)).toHaveText(before.filter((_, i) => i !== position))
        await expect(page.getByText('fresh_0', { exact: true })).toHaveCount(0)
      })
    }
  }
})
