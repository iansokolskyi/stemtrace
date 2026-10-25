import { expect, type Page, test } from '@playwright/test'

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
  })

  test('the next page asks for the next offset with the same parameters', async ({ page }) => {
    const api = await setupMockApi(page, { useDefaults: false })
    api.addTasks(manyTasks(450))
    const lists = recordTaskLists(page)

    await page.goto('/executions?state=SUCCESS')
    await expect(page.getByText('app.jobs.task_0000', { exact: true })).toBeVisible()
    await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight))

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
