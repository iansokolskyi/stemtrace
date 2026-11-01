import { expect, type Locator, type Page, test } from '@playwright/test'

import { setupMockApi } from './fixtures/mock-api'
import { createChain, createMockEvent, type MockTask } from './fixtures/mock-data'
import { at, openTask, successTask, tokenColour } from './fixtures/task-page'

/**
 * The task page details of the prototype: the breadcrumb, "Open flow" only for a real flow, the
 * short worker host, the worker link in Details, output levels and sentence-case headings.
 * Mock mode only.
 */

test.skip(process.env.E2E_MODE === 'real', 'Mock-only test')

const header = (page: Page): Locator => page.getByTestId('task-header')
const crumbs = (page: Page): Locator => page.getByRole('navigation', { name: 'Breadcrumb' })
const details = (page: Page): Locator => page.getByRole('region', { name: 'Details', exact: true })

/** The URL's query as [key, value] pairs. */
const query = (href: string | null): [string, string][] => [
  ...new URL(href ?? '', 'http://x').searchParams,
]

test.describe('Breadcrumb on a task page', () => {
  test('reads Executions, the task name (its runs), then the short ID', async ({ page }) => {
    const task = await openTask(page, successTask())
    await expect(crumbs(page).getByRole('link')).toHaveText(['Executions', task.name])
    const last = crumbs(page).locator('span[aria-current="page"]')
    await expect(last).toHaveText(task.task_id.slice(0, 8))
    await expect(last).toHaveText(/^[0-9a-f]{8}$/)
    await expect(last).toHaveAttribute('title', task.task_id)
    await expect(last).toHaveClass(/font-mono/)
    await expect(crumbs(page).getByRole('link', { name: task.name })).toHaveClass(/font-mono/)
  })

  test('the name links to every run of the task', async ({ page }) => {
    const task = await openTask(page, successTask())
    const name = crumbs(page).getByRole('link', { name: task.name })
    await expect(name).toHaveAttribute('href', `/executions?name_exact=${task.name}`)
    await name.click()
    await expect(page).toHaveURL(`/executions?name_exact=${task.name}`)
    await expect(crumbs(page).locator('span[aria-current="page"]')).toHaveText(task.name)
  })

  test('while the task loads, or when it fails, only the short ID follows Executions', async ({
    page,
  }) => {
    await setupMockApi(page, { useDefaults: false })
    await page.goto('/tasks/0123456789abcdef')
    await expect(page.getByText('Failed to load task')).toBeVisible({ timeout: 15_000 })
    await expect(crumbs(page).getByRole('link')).toHaveText(['Executions'])
    await expect(crumbs(page).locator('span[aria-current="page"]')).toHaveText('01234567')
  })

  test('a long task name is cut and the page does not scroll sideways at 400px', async ({
    page,
  }) => {
    await page.setViewportSize({ width: 400, height: 800 })
    await openTask(page, successTask({ name: `${'very.long.module.path.'.repeat(6)}send_receipt` }))
    expect(
      await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth),
    ).toBe(true)
    await expect(crumbs(page).locator('span[aria-current="page"]')).toBeVisible()
  })
})

test.describe('Open flow', () => {
  test('is left out for a task that is its own flow root with no parent and no children', async ({
    page,
  }) => {
    await openTask(page, successTask())
    await expect(header(page).getByRole('link', { name: 'All runs of this task' })).toBeVisible()
    await expect(header(page).getByRole('link', { name: 'Open flow' })).toHaveCount(0)
  })

  test('is shown for the root of a chain and for a step in it', async ({ page }) => {
    const api = await setupMockApi(page, { useDefaults: false })
    const chain = createChain('app.chain', 2)
    api.addTasks(chain)
    for (const task of chain) {
      await page.goto(`/tasks/${task.task_id}`)
      await expect(header(page).getByRole('link', { name: 'Open flow' })).toHaveAttribute(
        'href',
        `/flows/${chain[0].task_id}`,
      )
    }
  })
})

test.describe('Worker', () => {
  test('the button says the short host and links to that worker`s runs', async ({ page }) => {
    await openTask(page, successTask({ hostname: 'celery@worker-billing-1' }))
    const worker = header(page).getByRole('link', { name: /^Worker/ })
    await expect(worker).toHaveText('Worker worker-billing-1')
    await expect(worker.locator('span[title]')).toHaveAttribute('title', 'celery@worker-billing-1')
    expect(query(await worker.getAttribute('href'))).toEqual([
      ['hostname', 'celery@worker-billing-1'],
    ])
  })

  test('a host without the celery@ prefix is shown whole', async ({ page }) => {
    await openTask(page, successTask({ hostname: 'batch-1' }))
    await expect(header(page).getByRole('link', { name: /^Worker/ })).toHaveText('Worker batch-1')
  })

  test('the Details worker is a link to the Executions of that worker', async ({ page }) => {
    await openTask(page, successTask({ hostname: 'celery@worker-billing-1' }))
    const link = details(page).getByRole('link', { name: 'celery@worker-billing-1' })
    expect(query(await link.getAttribute('href'))).toEqual([
      ['hostname', 'celery@worker-billing-1'],
    ])
    await link.click()
    await expect(page).toHaveURL(/\/executions\?hostname=celery%40worker-billing-1$/)
    await expect(page.locator('[data-chip="hostname"]')).toContainText('celery@worker-billing-1')
  })

  test('the Details worker row is absent without a worker', async ({ page }) => {
    await openTask(page, successTask({ hostname: null }))
    await expect(details(page).getByRole('link')).toHaveCount(0)
    await expect(details(page).getByText('Worker', { exact: true })).toHaveCount(0)
  })
})

test.describe('Output levels', () => {
  function taskWithOutput(output: string): MockTask {
    const task = successTask()
    const { task_id, name } = task
    task.events = [
      createMockEvent(task_id, name, 'STARTED', { timestamp: at(1), retries: 0 }),
      createMockEvent(task_id, name, 'SUCCESS', { timestamp: at(2), retries: 0, output }),
    ]
    return task
  }
  const OUTPUT = [
    'INFO app.jobs: started',
    'WARNING app.jobs: slow',
    'WARN short form',
    'ERROR app.jobs: boom',
    'CRITICAL app.jobs: down',
    'plain line with INFO and ERROR inside',
    'INFORMATION is not a level',
    '  ERROR indented is not leading',
  ].join('\n')

  test('colour the leading level token only, with tokens', async ({ page }) => {
    await openTask(page, taskWithOutput(OUTPUT))
    const text = page.getByTestId('output-text')
    // The text itself is untouched
    await expect(text).toHaveText(OUTPUT)
    const tokens = text.locator('span')
    await expect(tokens).toHaveText(['INFO', 'WARNING', 'WARN', 'ERROR', 'CRITICAL'])
    const muted = await tokenColour(page, 'muted')
    const retry = await tokenColour(page, 'retry-t')
    const failure = await tokenColour(page, 'failure-t')
    const colours = [muted, retry, retry, failure, failure]
    for (const [index, colour] of colours.entries()) {
      await expect(tokens.nth(index)).toHaveCSS('color', colour)
    }
    await expect(tokens.nth(1)).toHaveCSS('font-weight', '700')
    // The rest of the line keeps the output colour
    await expect(text).toHaveCSS('color', await tokenColour(page, 'ink'))
  })

  test('output without a level has no tokens', async ({ page }) => {
    await openTask(page, taskWithOutput('hello\nworld'))
    await expect(page.getByTestId('output-text')).toHaveText('hello\nworld')
    await expect(page.getByTestId('output-text').locator('span')).toHaveCount(0)
  })

  test('the colours follow the dark theme', async ({ page }) => {
    await page.emulateMedia({ colorScheme: 'dark' })
    await openTask(page, taskWithOutput('ERROR app: boom'))
    await expect(page.getByTestId('output-text').locator('span')).toHaveCSS(
      'color',
      await tokenColour(page, 'failure-t'),
    )
  })
})

test.describe('Headings', () => {
  test('are sentence case', async ({ page }) => {
    const api = await setupMockApi(page, { useDefaults: false })
    const chain = createChain('app.chain', 2)
    api.addTasks(chain)
    await page.goto(`/tasks/${chain[0].task_id}`)
    const headings = page.locator('main h2')
    await expect(headings.filter({ hasText: /^Event history$/ })).toHaveCount(1)
    await expect(headings.filter({ hasText: /^Execution timeline$/ })).toHaveCount(1)
    await expect(headings.filter({ hasText: /^Child tasks$/ })).toHaveCount(1)
    await expect(
      headings.filter({ hasText: /Event History|Execution Timeline|Child Tasks/ }),
    ).toHaveCount(0)
  })
})
