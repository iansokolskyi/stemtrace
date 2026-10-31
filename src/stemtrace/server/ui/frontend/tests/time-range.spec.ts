import { expect, type Page, test } from '@playwright/test'

import { setupMockApi } from './fixtures/mock-api'
import { createSuccessTask, type MockTask } from './fixtures/mock-data'

/**
 * The time range switch in the top strip (1h, 6h, All) and the Executions header it feeds.
 * Mock mode only: it controls the API and reads the requests the page makes.
 */

test.skip(process.env.E2E_MODE === 'real', 'Mock-only test')

/** `count` successful tasks, the first the most recently updated. */
function manyTasks(count: number): MockTask[] {
  const base = Date.parse('2026-10-17T12:00:00Z')
  return Array.from({ length: count }, (_, index) => {
    const task = createSuccessTask(`app.jobs.task_${String(index).padStart(4, '0')}`)
    task.last_updated = new Date(base - index * 1000).toISOString()
    return task
  })
}

/** The query of every GET /api/tasks list request, in order. */
function recordTaskLists(page: Page): Record<string, string>[] {
  const seen: Record<string, string>[] = []
  page.on('request', (request) => {
    const url = new URL(request.url())
    if (url.pathname === '/api/tasks') seen.push(Object.fromEntries(url.searchParams))
  })
  return seen
}

/**
 * Answers GET /api/tasks with a total that depends on the window asked for (1h: 1, 6h: 3,
 * no window: 5; explicit dates: 2), so a cached answer for the wrong window shows in the page.
 */
async function answerByWindow(page: Page): Promise<void> {
  await page.route(/\/api\/tasks(\?|$)/, async (route) => {
    const url = new URL(route.request().url())
    const { within, from_date, to_date } = Object.fromEntries(url.searchParams)
    let total = 5
    if (from_date || to_date) total = 2
    else if (within === '1h') total = 1
    else if (within === '6h') total = 3
    const tasks = manyTasks(total).map((t) => ({
      ...t,
      exception_type: null,
      hostname: null,
      queue: null,
      task_group: null,
      tags: [],
      events: null,
    }))
    await route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({ tasks, total, limit: 200, offset: 0 }),
    })
  })
}

const search = (page: Page): Record<string, string> =>
  Object.fromEntries(new URL(page.url()).searchParams)

const LIST = { limit: '200', offset: '0', compact: '1' }
const TIMELINE = { limit: '50' }

const segment = (page: Page, name: '1h' | '6h' | 'All') =>
  page.getByRole('group', { name: 'Time range' }).getByRole('button', { name, exact: true })

/** Which segments are pressed, in order 1h, 6h, All. */
async function pressed(page: Page): Promise<boolean[]> {
  const states: boolean[] = []
  for (const name of ['1h', '6h', 'All'] as const) {
    states.push((await segment(page, name).getAttribute('aria-pressed')) === 'true')
  }
  return states
}

async function openExecutions(page: Page, path: string, count = 5) {
  const api = await setupMockApi(page, { useDefaults: false })
  api.addTasks(manyTasks(count))
  const lists = recordTaskLists(page)
  await page.goto(path)
  await expect.poll(() => lists.length).toBeGreaterThan(0)
  await expect(page.getByRole('group', { name: 'Time range' })).toBeVisible()
  return { api, lists }
}

test.describe('Time range requests', () => {
  const cases = [
    { name: 'no range in the URL is 6h', path: '', within: '6h', on: [false, true, false] },
    { name: 'range=1h', path: '?range=1h', within: '1h', on: [true, false, false] },
    { name: 'range=6h', path: '?range=6h', within: '6h', on: [false, true, false] },
    {
      name: 'range=all sends no window',
      path: '?range=all',
      within: undefined,
      on: [false, false, true],
    },
    { name: 'an unknown range is 6h', path: '?range=2d', within: '6h', on: [false, true, false] },
  ]
  for (const { name, path, within, on } of cases) {
    test(`${name}: the full list request and the pressed segment`, async ({ page }) => {
      const { lists } = await openExecutions(page, `/executions${path}`)
      // The full parameter set, not a substring: nothing extra, nothing missing
      expect(lists[0]).toEqual({ ...LIST, ...(within ? { within } : {}) })
      expect(await pressed(page)).toEqual(on)
    })

    test(`${name}: the full timeline request`, async ({ page }) => {
      const { lists } = await openExecutions(
        page,
        `/executions?view=timeline${path ? `&${path.slice(1)}` : ''}`,
      )
      expect(lists[0]).toEqual({ ...TIMELINE, ...(within ? { within } : {}) })
      expect(await pressed(page)).toEqual(on)
    })
  }

  test('explicit dates are sent alone: no window, and no segment is pressed', async ({ page }) => {
    const { lists } = await openExecutions(
      page,
      '/executions?range=1h&from_date=2026-10-01&to_date=2026-10-02',
    )
    expect(lists[0]).toEqual({ ...LIST, from_date: '2026-10-01', to_date: '2026-10-02' })
    expect(await pressed(page)).toEqual([false, false, false])
  })

  test('one explicit date is enough to drop the window', async ({ page }) => {
    const { lists } = await openExecutions(page, '/executions?to_date=2026-10-02')
    expect(lists[0]).toEqual({ ...LIST, to_date: '2026-10-02' })
    expect(await pressed(page)).toEqual([false, false, false])
  })
})

test.describe('Time range control', () => {
  test('choosing a segment writes the URL and the request; 6h removes the param', async ({
    page,
  }) => {
    await setupMockApi(page, { useDefaults: false })
    await answerByWindow(page)
    const lists = recordTaskLists(page)
    await page.goto('/executions')
    const subtitle = page.getByRole('heading', { level: 1 }).locator('xpath=following-sibling::p')
    await expect(subtitle).toHaveText('3 runs in the last 6h')
    expect(lists).toEqual([{ ...LIST, within: '6h' }])

    await segment(page, '1h').click()
    await expect.poll(() => search(page)).toEqual({ range: '1h' })
    await expect.poll(() => lists.at(-1)).toEqual({ ...LIST, within: '1h' })
    expect(await pressed(page)).toEqual([true, false, false])

    await segment(page, 'All').click()
    await expect.poll(() => search(page)).toEqual({ range: 'all' })
    await expect.poll(() => lists.at(-1)).toEqual(LIST)
    expect(await pressed(page)).toEqual([false, false, true])

    // 6h is cached from the first load: what shows is its answer, with no param in the URL
    await segment(page, '6h').click()
    await expect.poll(() => search(page)).toEqual({})
    await expect(subtitle).toHaveText('3 runs in the last 6h')
    expect(await pressed(page)).toEqual([false, true, false])
  })

  test('Back restores the previous range, URL and the rows for it', async ({ page }) => {
    await setupMockApi(page, { useDefaults: false })
    await answerByWindow(page)
    await page.goto('/executions')
    const subtitle = page.getByRole('heading', { level: 1 }).locator('xpath=following-sibling::p')
    await expect(subtitle).toHaveText('3 runs in the last 6h')
    await segment(page, '1h').click()
    await expect(subtitle).toHaveText('1 run in the last hour')
    await segment(page, 'All').click()
    await expect(subtitle).toHaveText('5 runs')
    expect(search(page)).toEqual({ range: 'all' })

    await page.goBack()
    await expect.poll(() => search(page)).toEqual({ range: '1h' })
    await expect(subtitle).toHaveText('1 run in the last hour')
    expect(await pressed(page)).toEqual([true, false, false])

    await page.goBack()
    await expect.poll(() => search(page)).toEqual({})
    await expect(subtitle).toHaveText('3 runs in the last 6h')
    expect(await pressed(page)).toEqual([false, true, false])
  })

  test('a reload keeps the range', async ({ page }) => {
    await openExecutions(page, '/executions?range=all')
    await page.reload()
    await expect(segment(page, 'All')).toHaveAttribute('aria-pressed', 'true')
    expect(await pressed(page)).toEqual([false, false, true])
  })

  test('the segments are buttons reached by Tab and activated by Enter and Space', async ({
    page,
  }) => {
    const { lists } = await openExecutions(page, '/executions')
    const focused = (name: string) =>
      page.evaluate((n) => document.activeElement?.textContent === n, name)
    // Tab from the top of the page reaches the control, before the page's own controls
    let reached = false
    for (let i = 0; i < 30 && !reached; i++) {
      await page.keyboard.press('Tab')
      reached = await focused('1h')
    }
    expect(reached).toBe(true)
    await page.keyboard.press('Enter')
    await expect.poll(() => search(page)).toEqual({ range: '1h' })
    await expect.poll(() => lists.at(-1)).toEqual({ ...LIST, within: '1h' })
    await expect(segment(page, '1h')).toBeFocused()

    await page.keyboard.press('Tab')
    await expect(segment(page, '6h')).toBeFocused()
    await page.keyboard.press('Tab')
    await expect(segment(page, 'All')).toBeFocused()
    await page.keyboard.press('Space')
    await expect.poll(() => search(page)).toEqual({ range: 'all' })
    await expect.poll(() => lists.at(-1)).toEqual(LIST)
    await page.keyboard.press('Shift+Tab')
    await page.keyboard.press('Enter')
    await expect.poll(() => search(page)).toEqual({})
    // The 6h answer is cached from the first load, so no new request is needed
    await expect(segment(page, '6h')).toHaveAttribute('aria-pressed', 'true')
  })

  test('the filters keep the range, and Clear does not reset it', async ({ page }) => {
    await openExecutions(page, '/executions?range=1h')
    await page.getByRole('button', { name: 'Add filter' }).click()
    await page.getByRole('dialog').getByRole('button', { name: 'Queue', exact: true }).click()
    await page.keyboard.type('payments')
    await page.keyboard.press('Enter')
    await expect.poll(() => search(page)).toEqual({ range: '1h', queue: 'payments' })

    await page.getByRole('button', { name: 'Clear', exact: true }).click()
    await expect.poll(() => search(page)).toEqual({ range: '1h' })
    expect(await pressed(page)).toEqual([true, false, false])
  })

  test('the view toggle keeps the range', async ({ page }) => {
    await openExecutions(page, '/executions?range=all')
    await page.getByRole('button', { name: 'Timeline' }).click()
    await expect.poll(() => search(page)).toEqual({ range: 'all', view: 'timeline' })
    expect(await pressed(page)).toEqual([false, false, true])
  })
})

test.describe('Dates and range', () => {
  test('choosing a range removes the dates from the URL and the request', async ({ page }) => {
    const { lists } = await openExecutions(
      page,
      '/executions?from_date=2026-10-01&to_date=2026-10-02&state=SUCCESS',
    )
    await segment(page, '1h').click()
    await expect.poll(() => search(page)).toEqual({ state: 'SUCCESS', range: '1h' })
    await expect.poll(() => lists.at(-1)).toEqual({ ...LIST, state: 'SUCCESS', within: '1h' })
    expect(await pressed(page)).toEqual([true, false, false])
    await expect(page.locator('[data-chip="time"]')).toHaveCount(0)

    await page.goBack()
    await expect
      .poll(() => search(page))
      .toEqual({
        from_date: '2026-10-01',
        to_date: '2026-10-02',
        state: 'SUCCESS',
      })
    expect(await pressed(page)).toEqual([false, false, false])
  })

  test('choosing 6h also replaces the dates', async ({ page }) => {
    const { lists } = await openExecutions(page, '/executions?from_date=2026-10-01')
    await segment(page, '6h').click()
    await expect.poll(() => search(page)).toEqual({})
    await expect.poll(() => lists.at(-1)).toEqual({ ...LIST, within: '6h' })
    expect(await pressed(page)).toEqual([false, true, false])
  })

  test('applying dates in the time chip removes the range', async ({ page }) => {
    await setupMockApi(page, { useDefaults: false })
    await answerByWindow(page)
    const lists = recordTaskLists(page)
    await page.goto('/executions?range=1h')
    const subtitle = page.getByRole('heading', { level: 1 }).locator('xpath=following-sibling::p')
    await expect(subtitle).toHaveText('1 run in the last hour')
    await page.getByRole('button', { name: 'Add filter' }).click()
    await page.getByRole('dialog').getByRole('button', { name: 'Time', exact: true }).click()
    await page.getByRole('dialog').getByLabel('From').focus()
    await page.keyboard.type('10012025')
    await page.getByRole('dialog').getByLabel('To').focus()
    await page.keyboard.type('10072025')
    await page.getByRole('dialog').getByRole('button', { name: 'Apply' }).click()

    await expect
      .poll(() => search(page))
      .toEqual({ from_date: '2025-10-01', to_date: '2025-10-07' })
    await expect
      .poll(() => lists.at(-1))
      .toEqual({ ...LIST, from_date: '2025-10-01', to_date: '2025-10-07' })
    await expect(subtitle).toHaveText('2 runs')
    expect(await pressed(page)).toEqual([false, false, false])

    // Back is the range again; its answer may come from the cache, so read the page, not the wire
    await page.goBack()
    await expect.poll(() => search(page)).toEqual({ range: '1h' })
    await expect(subtitle).toHaveText('1 run in the last hour')
    expect(await pressed(page)).toEqual([true, false, false])
  })

  test('removing the time chip leaves the range the URL names, 6h when it has none', async ({
    page,
  }) => {
    const { lists } = await openExecutions(page, '/executions?from_date=2026-10-01')
    await page.getByRole('button', { name: 'Remove time filter' }).click()
    await expect.poll(() => search(page)).toEqual({})
    await expect.poll(() => lists.at(-1)).toEqual({ ...LIST, within: '6h' })
    expect(await pressed(page)).toEqual([false, true, false])

    // A hand-written URL with both: the dates win until they go, then its range shows
    await page.goto('/executions?range=all&from_date=2026-10-01&to_date=2026-10-02')
    await expect(page.locator('[data-chip="time"]')).toBeVisible()
    expect(await pressed(page)).toEqual([false, false, false])
    await page.getByRole('button', { name: 'Remove time filter' }).click()
    await expect.poll(() => search(page)).toEqual({ range: 'all' })
    await expect.poll(() => lists.at(-1)).toEqual(LIST)
    expect(await pressed(page)).toEqual([false, false, true])
  })

  test('with the time popover open, a segment applies and closes it', async ({ page }) => {
    const { lists } = await openExecutions(page, '/executions?from_date=2026-10-01')
    await page.locator('[data-chip="time"] button').first().click()
    await expect(page.getByRole('dialog')).toBeVisible()
    const box = await segment(page, 'All').boundingBox()
    if (!box) throw new Error('no box for the All segment')
    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2)
    await page.mouse.down()
    await page.mouse.up()
    await expect.poll(() => search(page)).toEqual({ range: 'all' })
    await expect.poll(() => lists.at(-1)).toEqual(LIST)
    await expect(page.getByRole('dialog')).toHaveCount(0)
  })
})

test.describe('The range survives navigation', () => {
  const GROUPS = [
    { name: 'Billing', source: 'code' as const, tags: ['money'], color: null, task_names: [] },
  ]
  const ALL_REQUEST = { ...LIST }

  test('the Executions nav item, a group link and a tag link keep the range', async ({ page }) => {
    const api = await setupMockApi(page, { useDefaults: false })
    api.setGroups(GROUPS)
    api.addTasks(manyTasks(3))
    const lists = recordTaskLists(page)
    await page.goto('/executions?range=all')
    await expect.poll(() => lists.length).toBeGreaterThan(0)
    const nav = page.getByRole('navigation', { name: 'Sections' })
    // Leave for another page and come back through the nav item
    await nav.getByRole('link', { name: /Workers/ }).click()
    await expect(page.getByRole('heading', { level: 1, name: 'Workers' })).toBeVisible()
    await nav.getByRole('link', { name: /^Executions/ }).click()
    await expect.poll(() => search(page)).toEqual({ range: 'all' })
    await expect(segment(page, 'All')).toHaveAttribute('aria-pressed', 'true')

    await page
      .getByRole('navigation', { name: 'Groups' })
      .getByRole('link', { name: 'Billing' })
      .click()
    await expect.poll(() => search(page)).toEqual({ task_group: 'Billing', range: 'all' })
    await expect.poll(() => lists.at(-1)).toEqual({ ...ALL_REQUEST, task_group: 'Billing' })

    await page
      .getByRole('navigation', { name: 'Tags' })
      .getByRole('link', { name: '#money' })
      .click()
    await expect.poll(() => search(page)).toEqual({ tag: 'money', range: 'all' })
    await expect.poll(() => lists.at(-1)).toEqual({ ...ALL_REQUEST, tag: 'money' })
  })

  test('the Task page links back to Executions with the range', async ({ page }) => {
    const api = await setupMockApi(page, { useDefaults: false })
    const task = createSuccessTask('app.jobs.one')
    task.hostname = 'celery@w1'
    api.addTasks([task])
    await page.goto('/executions?range=1h')
    await expect(page.getByRole('group', { name: 'Time range' })).toBeVisible()
    await page
      .getByRole('link', { name: /app\.jobs/ })
      .first()
      .click()
    await expect(page).toHaveURL(/\/tasks\//)
    await page.getByRole('link', { name: /All runs of this task/ }).click()
    await expect.poll(() => search(page)).toEqual({ name_exact: 'app.jobs.one', range: '1h' })
    await expect(segment(page, '1h')).toHaveAttribute('aria-pressed', 'true')
  })

  test('the default range adds nothing to the links', async ({ page }) => {
    await openExecutions(page, '/executions?range=1h')
    await segment(page, '6h').click()
    await expect.poll(() => search(page)).toEqual({})
    await page
      .getByRole('navigation', { name: 'Sections' })
      .getByRole('link', { name: /Workers/ })
      .click()
    await page
      .getByRole('navigation', { name: 'Sections' })
      .getByRole('link', { name: /^Executions/ })
      .click()
    await expect(page).toHaveURL(/\/executions$/)
  })
})

test.describe('Time range only where the page has one', () => {
  test('the Task page has no control, even with range in the URL', async ({ page }) => {
    const task = createSuccessTask('app.jobs.one')
    const api = await setupMockApi(page, { useDefaults: false })
    api.addTasks([task])
    await page.goto(`/tasks/${task.task_id}?range=1h`)
    await expect(page.getByRole('heading', { level: 1 })).toBeVisible()
    await expect(page.getByRole('group', { name: 'Time range' })).toHaveCount(0)
    await expect(page.getByRole('button', { name: '1h', exact: true })).toHaveCount(0)
  })

  for (const path of ['/flows', '/workers', '/registry']) {
    test(`${path} has no control`, async ({ page }) => {
      await setupMockApi(page)
      await page.goto(`${path}?range=1h`)
      await expect(page.getByRole('heading', { level: 1 })).toBeVisible()
      await expect(page.getByRole('group', { name: 'Time range' })).toHaveCount(0)
    })
  }

  test('the control follows the route: gone after leaving Executions, back on return', async ({
    page,
  }) => {
    await openExecutions(page, '/executions')
    await expect(page.getByRole('group', { name: 'Time range' })).toBeVisible()
    await page
      .getByRole('navigation', { name: 'Sections' })
      .getByRole('link', { name: /Workers/ })
      .click()
    await expect(page.getByRole('heading', { level: 1, name: 'Workers' })).toBeVisible()
    await expect(page.getByRole('group', { name: 'Time range' })).toHaveCount(0)
    await page.goBack()
    await expect(page.getByRole('group', { name: 'Time range' })).toBeVisible()
  })
})

test.describe('Executions header', () => {
  const heading = (page: Page) => page.getByRole('heading', { level: 1 })
  const subtitle = (page: Page) => heading(page).locator('xpath=following-sibling::p')

  test('the title is Executions', async ({ page }) => {
    await openExecutions(page, '/executions')
    await expect(heading(page)).toHaveText('Executions')
  })

  const cases = [
    { path: '', text: '2,000 runs in the last 6h' },
    { path: '?range=6h', text: '2,000 runs in the last 6h' },
    { path: '?range=1h', text: '2,000 runs in the last hour' },
    { path: '?range=all', text: '2,000 runs' },
    { path: '?from_date=2026-10-01', text: '2,000 runs' },
    { path: '?range=1h&to_date=2026-10-01', text: '2,000 runs' },
  ]
  for (const { path, text } of cases) {
    test(`the subtitle for ${path || 'no params'} is "${text}"`, async ({ page }) => {
      await openExecutions(page, `/executions${path}`, 2000)
      await expect(subtitle(page)).toHaveText(text)
    })
  }

  test('the timeline view says the same', async ({ page }) => {
    await openExecutions(page, '/executions?view=timeline&range=1h', 2000)
    await expect(subtitle(page)).toHaveText('2,000 runs in the last hour')
  })

  test('the count is the API total after filtering, and follows the range switch', async ({
    page,
  }) => {
    const { api } = await openExecutions(page, '/executions', 3)
    api.addTask({ ...createSuccessTask('other.thing'), last_updated: '2026-10-17T11:00:00Z' })
    await page.goto('/executions?name=app.jobs&range=1h')
    await expect(subtitle(page)).toHaveText('3 runs in the last hour')
    await segment(page, 'All').click()
    await expect(subtitle(page)).toHaveText('3 runs')
  })

  test('one run is "1 run"', async ({ page }) => {
    await openExecutions(page, '/executions', 1)
    await expect(subtitle(page)).toHaveText('1 run in the last 6h')
  })
})

test.describe('Narrow screens', () => {
  for (const path of ['/executions', '/executions?range=1h', '/executions?view=timeline']) {
    test(`${path} has no horizontal page scroll at 400px with the control present`, async ({
      page,
    }) => {
      await page.setViewportSize({ width: 400, height: 860 })
      await openExecutions(page, path, 30)
      await expect(page.getByRole('group', { name: 'Time range' })).toBeVisible()
      const overflow = await page.evaluate(
        () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
      )
      expect(overflow).toBeLessThanOrEqual(0)
      // All three segments are fully on screen
      for (const name of ['1h', '6h', 'All'] as const) {
        const box = await segment(page, name).boundingBox()
        expect(box).not.toBeNull()
        expect((box?.x ?? -1) + (box?.width ?? 0)).toBeLessThanOrEqual(400)
        expect(box?.x ?? -1).toBeGreaterThanOrEqual(0)
      }
    })
  }
})
