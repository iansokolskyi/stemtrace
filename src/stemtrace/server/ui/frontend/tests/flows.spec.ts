import { expect, type Locator, type Page, test } from '@playwright/test'

import { FLOW_ROWS, FLOWS_NOW, flowListRoots } from './fixtures/flows-list'
import { setupMockApi } from './fixtures/mock-api'
import { createSuccessTask } from './fixtures/mock-data'

/**
 * Tests for the Flows list. The first group also runs against the real server (E2E_MODE=real);
 * the rest control the API and read the requests the page makes, so they are mock-only.
 */

const isRealMode = process.env.E2E_MODE === 'real'

const rows = (page: Page): Locator => page.getByTestId('flow-row')
const subtitle = (page: Page): Locator =>
  page.getByRole('heading', { level: 1 }).locator('xpath=following-sibling::p')
const segment = (page: Page, name: '1h' | '6h' | 'All'): Locator =>
  page.getByRole('group', { name: 'Time range' }).getByRole('button', { name, exact: true })

/** The query of every GET /api/graphs list request, in order. */
function recordGraphLists(page: Page): Record<string, string>[] {
  const seen: Record<string, string>[] = []
  page.on('request', (request) => {
    const url = new URL(request.url())
    if (url.pathname === '/api/graphs') seen.push(Object.fromEntries(url.searchParams))
  })
  return seen
}

/** The lines of text a row shows, top to bottom. */
async function lines(row: Locator): Promise<string[]> {
  return row.evaluate((el) =>
    // biome-ignore lint/performance/useDomNodeTextContent: the test reads the rendered lines, which textContent does not have
    (el as HTMLElement).innerText.split('\n').filter((line) => line.trim()),
  )
}

test.describe('Flows list', () => {
  test.beforeEach(async ({ page }) => {
    if (!isRealMode) await setupMockApi(page)
  })

  test('shows the Flows heading', async ({ page }) => {
    await page.goto('/flows')
    await expect(page.getByRole('heading', { level: 1, name: 'Flows' })).toBeVisible()
  })

  test('lists a link per flow once the flows exist', async ({ page }) => {
    await page.goto('/flows')
    // The list loads after the page does
    await expect(page.locator('a[href*="/flows/"]').first()).toBeVisible({ timeout: 15_000 })
  })

  test('opens the flow page from a row', async ({ page }) => {
    await page.goto('/flows')
    const first = page.locator('a[href*="/flows/"]').first()
    await expect(first).toBeVisible({ timeout: 15_000 })
    await first.click()
    await expect(page).toHaveURL(/\/flows\/.+/)
  })
})

test.describe('Flows list - mocked flows', () => {
  test.skip(isRealMode, 'Mock-only test')

  test.beforeEach(async ({ page }) => {
    await page.clock.setFixedTime(new Date(FLOWS_NOW))
    const api = await setupMockApi(page, { useDefaults: false })
    api.addTasks(flowListRoots())
  })

  test('each row says its state, title, summary, root and age', async ({ page }) => {
    await page.goto('/flows')
    await expect(rows(page)).toHaveCount(FLOW_ROWS.length)
    for (const [index, expected] of FLOW_ROWS.entries()) {
      const row = rows(page).nth(index)
      await expect(row.locator('[data-state]')).toHaveAttribute('data-state', expected.state)
      expect(await lines(row), expected.taskId).toEqual([
        expected.stateWord,
        expected.title,
        ...(expected.summary ? [expected.summary] : []),
        'root',
        // The module path, then the last segment, as two lines: the name is two spans
        ...expected.rootName.split(/(?<=\.)(?=[^.]+$)/),
        expected.age,
      ])
    }
  })

  test('failure is loud and the quiet states are not', async ({ page }) => {
    await page.goto('/flows')
    await expect(rows(page)).toHaveCount(FLOW_ROWS.length)
    const variants = await rows(page).evaluateAll((els) =>
      els.map((el) => el.querySelector('[data-state]')?.getAttribute('data-variant')),
    )
    // Failure, Success, Running, then Retry (from the root, as the flow has no summary)
    expect(variants).toEqual(['loud', 'quiet', 'quiet', 'loud'])
  })

  test('a flow with no summary shows the root state and no summary line', async ({ page }) => {
    await page.goto('/flows')
    const row = rows(page).nth(3)
    await expect(row.locator('[data-state]')).toHaveAttribute('data-state', 'RETRY')
    await expect(row).not.toContainText('tasks')
    await expect(row).not.toContainText('failed')
  })

  test('the header counts the flows in the window, with separators', async ({ page }) => {
    await page.goto('/flows')
    await expect(subtitle(page)).toHaveText('4 flows in the last 6h')
  })

  test('the header formats the total from the API with separators', async ({ page }) => {
    await page.route(/\/api\/graphs(\?|$)/, async (route) => {
      const [only] = flowListRoots()
      await route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({
          graphs: [{ ...only, children: [] }],
          total: 1204,
          limit: 50,
          offset: 0,
        }),
      })
    })
    await page.goto('/flows?range=1h')
    await expect(subtitle(page)).toHaveText('1,204 flows in the last hour')
  })

  test('a row is a link to its flow, reached by Tab and opened by Enter', async ({ page }) => {
    await page.goto('/flows')
    const first = rows(page).first()
    expect(await first.evaluate((el) => el.tagName)).toBe('A')
    await expect(first).toHaveAttribute('href', '/flows/flow-chord-0001')

    await first.focus()
    await expect(first).toBeFocused()
    await page.keyboard.press('Enter')
    await expect(page).toHaveURL(/\/flows\/flow-chord-0001$/)
  })

  test('the second row is reached by Tab from the first', async ({ page }) => {
    await page.goto('/flows')
    await rows(page).first().focus()
    await page.keyboard.press('Tab')
    await expect(rows(page).nth(1)).toBeFocused()
  })

  test('shows no horizontal page scroll at 400px', async ({ page }) => {
    await page.setViewportSize({ width: 400, height: 860 })
    await page.goto('/flows')
    await expect(rows(page)).toHaveCount(FLOW_ROWS.length)
    const overflow = await page.evaluate(() => ({
      scroll: document.documentElement.scrollWidth,
      client: document.documentElement.clientWidth,
    }))
    expect(overflow.scroll).toBeLessThanOrEqual(overflow.client)
  })

  test('shows the empty state for the window', async ({ page }) => {
    const api = await setupMockApi(page, { useDefaults: false })
    api.clearTasks()
    await page.goto('/flows')
    await expect(page.getByText('No flows in the last 6h.', { exact: true })).toBeVisible()
    await expect(subtitle(page)).toHaveText('0 flows in the last 6h')

    await segment(page, '1h').click()
    await expect(page.getByText('No flows in the last hour.', { exact: true })).toBeVisible()

    await segment(page, 'All').click()
    await expect(page.getByText('No flows.', { exact: true })).toBeVisible()
    await expect(subtitle(page)).toHaveText('0 flows')
  })

  test('a failed request says so', async ({ page }) => {
    await page.route(/\/api\/graphs(\?|$)/, (route) => route.fulfill({ status: 500, body: '{}' }))
    await page.goto('/flows')
    // The query retries three times with backoff before it gives up
    await expect(page.getByText('Failed to load flows', { exact: true })).toBeVisible({
      timeout: 15_000,
    })
  })
})

test.describe('Flows list - time range', () => {
  test.skip(isRealMode, 'Mock-only test')

  test.beforeEach(async ({ page }) => {
    const api = await setupMockApi(page, { useDefaults: false })
    api.addTasks(flowListRoots())
  })

  async function open(page: Page, path: string): Promise<Record<string, string>[]> {
    const lists = recordGraphLists(page)
    await page.goto(path)
    await expect(rows(page).first()).toBeVisible()
    await expect(page.getByRole('group', { name: 'Time range' })).toBeVisible()
    return lists
  }

  // The first page of the list: the limit, and no offset (0 is not sent)
  const PAGE = { limit: '50' }

  const cases = [
    { name: 'no range in the URL is 6h', path: '', within: '6h', on: [false, true, false] },
    { name: 'range=1h', path: '?range=1h', within: '1h', on: [true, false, false] },
    { name: 'range=6h', path: '?range=6h', within: '6h', on: [false, true, false] },
    { name: 'range=all sends no window', path: '?range=all', on: [false, false, true] },
    { name: 'an unknown range is 6h', path: '?range=2d', within: '6h', on: [false, true, false] },
  ]
  for (const { name, path, within, on } of cases) {
    test(`${name}: the full request and the pressed segment`, async ({ page }) => {
      const lists = await open(page, `/flows${path}`)
      expect(lists[0]).toEqual({ ...PAGE, ...(within ? { within } : {}) })
      const pressed = await Promise.all(
        (['1h', '6h', 'All'] as const).map(
          async (label) => (await segment(page, label).getAttribute('aria-pressed')) === 'true',
        ),
      )
      expect(pressed).toEqual(on)
    })
  }

  test('explicit dates are sent alone, even next to a range', async ({ page }) => {
    const lists = await open(page, '/flows?range=1h&from_date=2026-10-01&to_date=2026-10-02')
    expect(lists[0]).toEqual({ ...PAGE, from_date: '2026-10-01', to_date: '2026-10-02' })
    await expect(segment(page, '1h')).toHaveAttribute('aria-pressed', 'false')
    await expect(subtitle(page)).toHaveText('4 flows')
  })

  test('one explicit date is enough to drop the window', async ({ page }) => {
    const lists = await open(page, '/flows?to_date=2026-10-02')
    expect(lists[0]).toEqual({ ...PAGE, to_date: '2026-10-02' })
  })

  test('choosing a segment writes the URL and the request', async ({ page }) => {
    const lists = await open(page, '/flows')
    await segment(page, '1h').click()
    await expect.poll(() => new URL(page.url()).search).toBe('?range=1h')
    await expect.poll(() => lists.at(-1)).toEqual({ ...PAGE, within: '1h' })
    await expect(subtitle(page)).toHaveText('4 flows in the last hour')

    await segment(page, 'All').click()
    await expect.poll(() => lists.at(-1)).toEqual(PAGE)
    await expect(subtitle(page)).toHaveText('4 flows')
  })

  test('the range follows you out of Flows and back through the sidebar', async ({ page }) => {
    await open(page, '/flows?range=1h')
    const nav = page.getByRole('navigation', { name: 'Sections' })
    await nav.getByRole('link', { name: 'Executions' }).click()
    await expect(page).toHaveURL(/\/executions\?range=1h$/)
    await nav.getByRole('link', { name: 'Flows' }).click()
    await expect(page).toHaveURL(/\/flows\?range=1h$/)
    await expect(segment(page, '1h')).toHaveAttribute('aria-pressed', 'true')
  })

  test('the range chosen on Executions applies to Flows', async ({ page }) => {
    await page.goto('/executions?range=all')
    await segment(page, 'All').waitFor()
    const lists = recordGraphLists(page)
    await page
      .getByRole('navigation', { name: 'Sections' })
      .getByRole('link', { name: 'Flows' })
      .click()
    await expect(page).toHaveURL(/\/flows\?range=all$/)
    await expect.poll(() => lists[0]).toEqual(PAGE)
  })
})

test.describe('Flows list - paging and live updates', () => {
  test.skip(isRealMode, 'Mock-only test')

  test('loads the next page when the last row scrolls into view', async ({ page }) => {
    const roots = Array.from({ length: 60 }, (_, index) => {
      const task = createSuccessTask(`app.jobs.job_${String(index).padStart(2, '0')}`)
      return Object.assign(task, { task_id: `flow-page-${index}`, root_id: `flow-page-${index}` })
    })
    await setupMockApi(page, { useDefaults: false })
    const lists: Record<string, string>[] = []
    await page.route(/\/api\/graphs(\?|$)/, async (route) => {
      const query = Object.fromEntries(new URL(route.request().url()).searchParams)
      lists.push(query)
      const offset = Number(query.offset ?? 0)
      await route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({
          graphs: roots.slice(offset, offset + 50).map((root) => ({ ...root, children: [] })),
          total: 60,
          limit: 50,
          offset,
        }),
      })
    })
    await page.goto('/flows')
    await expect(rows(page)).toHaveCount(50)
    await rows(page).last().scrollIntoViewIfNeeded()
    await expect(rows(page)).toHaveCount(60)
    expect(lists.map((query) => query.offset)).toEqual([undefined, '50'])
    await expect(subtitle(page)).toHaveText('60 flows in the last 6h')
  })

  test('shows a new flow by polling while the socket is down', async ({ page }) => {
    const api = await setupMockApi(page, { useDefaults: false })
    const [chord, chain] = flowListRoots()
    api.addTasks([chain])
    // A socket the server closes at once: the page falls back to polling
    await page.routeWebSocket(/\/ws$/, (ws) => ws.close())
    await page.goto('/flows')
    await expect(rows(page)).toHaveCount(1)
    await expect(rows(page).first().locator('[data-state]')).toHaveAttribute(
      'data-state',
      'SUCCESS',
    )

    api.addTask(chord)
    // The page polls every 5 s
    await expect(rows(page)).toHaveCount(2, { timeout: 15_000 })
    await expect(subtitle(page)).toHaveText('2 flows in the last 6h')
  })
})
