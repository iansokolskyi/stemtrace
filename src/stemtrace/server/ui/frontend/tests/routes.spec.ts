import { expect, type Page, test } from '@playwright/test'

import { setupMockApi } from './fixtures/mock-api'
import { createChain, createFailedTask } from './fixtures/mock-data'

/**
 * New routes and the redirects that keep old links working, both with the UI at the root and
 * under a mount prefix (an embedded app at /stemtrace, or a reverse proxy's --root-path).
 */

const isRealMode = process.env.E2E_MODE === 'real'

for (const base of ['', '/stemtrace']) {
  test.describe(`Routes (basepath "${base || '/'}")`, () => {
    test.skip(isRealMode, 'Mock-only test')

    async function open(page: Page, path: string): Promise<void> {
      if (base) {
        await page.addInitScript((b) => {
          window.__STEMTRACE_BASE__ = b
        }, base)
      }
      await page.goto(`${base}${path}`)
    }

    test('/graphs redirects to /flows and keeps the date filter', async ({ page }) => {
      await setupMockApi(page)
      await open(page, '/graphs?from_date=2026-01-01')
      await expect(page).toHaveURL(`${base}/flows?from_date=2026-01-01`)
      await expect(page.getByRole('heading', { level: 1 })).toBeVisible()
    })

    test('/graph/<root> redirects to /flows/<root> and draws the graph', async ({ page }) => {
      const api = await setupMockApi(page)
      const chain = createChain('tests.routes_chain', 2)
      api.addTasks(chain)
      const root = chain[0].task_id
      await open(page, `/graph/${root}`)
      await expect(page).toHaveURL(`${base}/flows/${root}`)
      await expect(page.locator('.react-flow__node').first()).toBeVisible()
      // The breadcrumb links back to the list.
      await expect(page.getByRole('navigation', { name: 'Breadcrumb' })).toContainText(root)
      await page
        .getByRole('navigation', { name: 'Breadcrumb' })
        .getByRole('link', { name: 'Flows' })
        .click()
      await expect(page).toHaveURL(`${base}/flows`)
    })

    test('/ with old task-list filters redirects to /executions with them', async ({ page }) => {
      const api = await setupMockApi(page)
      api.addTask(createFailedTask('tests.routes_failing', 'ValueError: no', 'Traceback: no'))
      await open(page, '/?state=FAILURE&name=routes_failing')
      await expect(page).toHaveURL(`${base}/executions?state=FAILURE&name=routes_failing`)
      await expect(page.getByText('routes_failing').first()).toBeVisible()
      await expect(page.getByText('tasks.add')).toHaveCount(0)
    })

    test('a filter that looks like a number survives the redirect', async ({ page }) => {
      await setupMockApi(page)
      const requests: string[] = []
      page.on('request', (r) => {
        if (new URL(r.url()).pathname.endsWith('/api/tasks')) requests.push(r.url())
      })
      await open(page, '/?name=123&state=FAILURE')
      await expect(page).toHaveURL(/\/executions\?/)
      await expect
        .poll(() => requests.some((u) => new URL(u).searchParams.get('name') === '123'))
        .toBe(true)
    })

    test('bare / opens Executions', async ({ page }) => {
      await setupMockApi(page)
      await open(page, '/')
      await expect(page).toHaveURL(`${base}/executions`)
      await expect(
        page
          .getByRole('navigation', { name: 'Sections' })
          .getByRole('link', { name: /Executions/ }),
      ).toHaveAttribute('aria-current', 'page')
    })

    test('task pages link back to Executions through the breadcrumb', async ({ page }) => {
      const api = await setupMockApi(page)
      const task = api.tasks[0]
      await open(page, `/tasks/${task.task_id}`)
      const crumbs = page.getByRole('navigation', { name: 'Breadcrumb' })
      await expect(crumbs).toContainText(task.task_id.slice(0, 8))
      await crumbs.getByRole('link', { name: 'Executions' }).click()
      await expect(page).toHaveURL(`${base}/executions`)
    })
  })
}
