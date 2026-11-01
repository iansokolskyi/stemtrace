import { expect, type Locator, type Page, test } from '@playwright/test'

import { setupMockApi } from './fixtures/mock-api'
import { createRegistryEntry, createSuccessTask } from './fixtures/mock-data'

/**
 * Every in-app link to Executions carries the range the page was showing (`?range=1h`), so the
 * range survives leaving the page. Each case starts from Executions on 1h, goes to the page that
 * holds the link by clicking (the range is kept in memory, not in the URL of the next page), and
 * checks the link's target and where it lands. Mock mode only.
 */

test.skip(process.env.E2E_MODE === 'real', 'Mock-only test')

const NAME = 'app.jobs.one'
const HOST = 'celery@w1'

/** The query of a URL or href as sorted [key, value] pairs. */
const query = (href: string | null): [string, string][] =>
  [...new URL(href ?? '', 'http://x').searchParams].sort(([a], [b]) => a.localeCompare(b))

interface LinkCase {
  name: string
  /** Opens the page that holds the link and returns it. */
  reach: (page: Page) => Promise<Locator>
  /** What the link asks of Executions besides `range=1h`. */
  filters: [string, string][]
}

async function openFromExecutions(page: Page): Promise<string> {
  const api = await setupMockApi(page, { useDefaults: false })
  const task = createSuccessTask(NAME)
  task.hostname = HOST
  api.addTasks([task])
  api.setRegistry([createRegistryEntry(NAME, 3)])
  await page.goto('/executions?range=1h')
  await expect(page.getByRole('group', { name: 'Time range' })).toBeVisible()
  return task.task_id
}

const openTask = async (page: Page): Promise<void> => {
  await page
    .getByRole('link', { name: /app\.jobs/ })
    .first()
    .click()
  await expect(page.getByRole('heading', { level: 1 })).toBeVisible()
}

const CASES: LinkCase[] = [
  {
    name: 'the breadcrumb Executions link on a task page',
    reach: async (page) => {
      await openFromExecutions(page)
      await openTask(page)
      return page.getByRole('navigation', { name: 'Breadcrumb' }).getByRole('link', {
        name: 'Executions',
      })
    },
    filters: [],
  },
  {
    name: 'the breadcrumb task name on a task page',
    reach: async (page) => {
      await openFromExecutions(page)
      await openTask(page)
      return page.getByRole('navigation', { name: 'Breadcrumb' }).getByRole('link', {
        name: NAME,
      })
    },
    filters: [['name_exact', NAME]],
  },
  {
    name: 'the Worker button in the task header',
    reach: async (page) => {
      await openFromExecutions(page)
      await openTask(page)
      return page.getByTestId('task-header').getByRole('link', { name: /^Worker/ })
    },
    filters: [['hostname', HOST]],
  },
  {
    name: 'the worker in the Details panel',
    reach: async (page) => {
      await openFromExecutions(page)
      await openTask(page)
      return page.getByRole('region', { name: 'Details', exact: true }).getByRole('link', {
        name: HOST,
      })
    },
    filters: [['hostname', HOST]],
  },
  {
    name: 'a task row in the Registry',
    reach: async (page) => {
      await openFromExecutions(page)
      await page
        .getByRole('navigation', { name: 'Sections' })
        .getByRole('link', { name: /^Registry/ })
        .click()
      return page.getByRole('link', { name: NAME, exact: true })
    },
    filters: [['name_exact', NAME]],
  },
  {
    name: 'Back to executions when the task page fails',
    reach: async (page) => {
      const id = await openFromExecutions(page)
      // The row's page fails to load (after the retries): the link below the error
      await page.route(new RegExp(`/api/tasks/${id}$`), (route) =>
        route.fulfill({ status: 500, contentType: 'application/json', body: '{}' }),
      )
      await page
        .getByRole('link', { name: /app\.jobs/ })
        .first()
        .click()
      const back = page.getByRole('link', { name: 'Back to executions' })
      await expect(back).toBeVisible({ timeout: 15_000 })
      return back
    },
    filters: [],
  },
  {
    name: 'the breadcrumb Executions link over one task name',
    reach: async (page) => {
      await openFromExecutions(page)
      await page.goto(`/executions?name_exact=${NAME}&range=1h`)
      await expect(page.getByRole('group', { name: 'Time range' })).toBeVisible()
      return page.getByRole('navigation', { name: 'Breadcrumb' }).getByRole('link')
    },
    filters: [],
  },
]

test.describe('Links to Executions carry the range', () => {
  for (const { name, reach, filters } of CASES) {
    test(name, async ({ page }) => {
      const link = await reach(page)
      const expected = [...filters, ['range', '1h'] as [string, string]].sort(([a], [b]) =>
        a.localeCompare(b),
      )
      expect(query(await link.getAttribute('href'))).toEqual(expected)
      await link.click()
      await expect(page).toHaveURL(/\/executions\?/)
      expect(query(page.url())).toEqual(expected)
      await expect(page.getByRole('button', { name: '1h', exact: true })).toHaveAttribute(
        'aria-pressed',
        'true',
      )
    })
  }
})
