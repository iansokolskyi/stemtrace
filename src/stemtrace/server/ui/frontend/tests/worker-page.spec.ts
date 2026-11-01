import { expect, type Page, test } from '@playwright/test'

import { type MockApiContext, setupMockApi } from './fixtures/mock-api'
import { createSuccessTask, type MockTask, type MockWorker } from './fixtures/mock-data'

/**
 * Tests for the Worker page (`/workers/<hostname>`): the worker's registered tasks, its recent
 * runs from `/api/tasks?hostname=`, and the facts the API knows. Mock-only.
 */

test.skip(process.env.E2E_MODE === 'real', 'Mock-only test')

const HOST = 'celery@alpha'

const minutesAgo = (minutes: number): string =>
  new Date(Date.now() - minutes * 60_000).toISOString()

function worker(overrides: Partial<MockWorker> = {}): MockWorker {
  return {
    hostname: HOST,
    pid: 4242,
    registered_tasks: ['app.billing.send', 'app.add'],
    status: 'online',
    registered_at: minutesAgo(180),
    last_seen: minutesAgo(2),
    ...overrides,
  }
}

function run(name: string, extra: Partial<MockTask> = {}): MockTask {
  return {
    ...createSuccessTask(name),
    hostname: HOST,
    queue: 'default',
    queue_wait_ms: 40,
    runtime_ms: 1200,
    ...extra,
  }
}

/** Opens the worker's page and waits for the refreshed workers list and the runs it shows. */
async function openWorker(page: Page, hostname = HOST): Promise<void> {
  const workers = page.waitForResponse((response) => {
    const url = new URL(response.url())
    return /\/api\/workers$/.test(url.pathname) && !url.searchParams.has('refresh')
  })
  await page.goto(`/workers/${encodeURIComponent(hostname)}`)
  await workers
}

const panel = (page: Page, name: string) => page.getByRole('region', { name })

test.describe('Worker page', () => {
  let api: MockApiContext

  test.beforeEach(async ({ page }) => {
    api = await setupMockApi(page, { useDefaults: false })
    api.setWorkers([worker()])
  })

  test('names the worker, shows its status and links to all its executions', async ({ page }) => {
    await openWorker(page)
    await expect(page.getByRole('heading', { name: HOST, level: 1 })).toBeVisible()
    await expect(page.getByTestId('worker-status')).toHaveText('Online')
    await expect(page.getByTestId('worker-status')).toHaveAttribute('data-variant', 'quiet')
    const all = page.getByRole('link', { name: 'All executions on this worker' })
    await expect(all).toHaveAttribute('href', /\/executions\?hostname=celery(@|%40)alpha/)
    await all.click()
    await expect(page).toHaveURL(/\/executions\?.*hostname=celery(@|%40)alpha/)
  })

  test('the link to all executions carries the range the page was reached with', async ({
    page,
  }) => {
    await page.goto('/executions?range=1h')
    await page.getByRole('link', { name: /Workers/ }).click()
    await page.getByRole('link', { name: HOST }).click()
    const all = page.getByRole('link', { name: 'All executions on this worker' })
    await expect(all).toHaveAttribute('href', /range=1h/)
  })

  test('the breadcrumb reads Workers then the hostname', async ({ page }) => {
    await openWorker(page)
    const crumb = page.getByRole('navigation', { name: 'Breadcrumb' })
    await expect(crumb.getByRole('link', { name: 'Workers' })).toHaveAttribute('href', '/workers')
    // Only the last crumb is the current page; the list above it is its parent
    await expect(crumb.getByRole('link', { name: 'Workers' })).not.toHaveAttribute('aria-current')
    await expect(crumb.locator('[aria-current="page"]')).toHaveText(HOST)
  })

  test('an offline worker is loud and explains itself with when it was last seen', async ({
    page,
  }) => {
    api.setWorkers([worker({ status: 'offline', last_seen: minutesAgo(65) })])
    await openWorker(page)
    await expect(page.getByTestId('worker-status')).toHaveText('Offline')
    await expect(page.getByTestId('worker-status')).toHaveAttribute('data-variant', 'loud')
    const callout = page.getByRole('note')
    await expect(callout).toContainText('Offline')
    await expect(callout).toContainText('Last seen')
    await expect(callout).toContainText('1h ago')
  })

  test('an online worker has no offline callout', async ({ page }) => {
    await openWorker(page)
    await expect(page.getByRole('heading', { name: HOST, level: 1 })).toBeVisible()
    await expect(page.getByRole('note')).toHaveCount(0)
  })

  test('lists the registered tasks, each opening Executions for that task', async ({ page }) => {
    await openWorker(page)
    const tasks = panel(page, 'Registered tasks')
    await expect(tasks.getByText('2 tasks')).toBeVisible()
    const billing = tasks.getByRole('link', { name: 'app.billing.send' })
    await expect(billing).toHaveAttribute('href', /\/executions\?.*name_exact=app\.billing\.send/)
    await billing.click()
    await expect(page).toHaveURL(/name_exact=app\.billing\.send/)
  })

  test('registered tasks show a Group column only when a group has the task', async ({ page }) => {
    await openWorker(page)
    await expect(
      panel(page, 'Registered tasks').getByRole('columnheader', { name: 'Group' }),
    ).toHaveCount(0)

    api.setGroups([
      { name: 'Billing', source: 'code', tags: [], color: null, task_names: ['app.billing.send'] },
    ])
    await openWorker(page)
    const tasks = panel(page, 'Registered tasks')
    await expect(tasks.getByRole('columnheader', { name: 'Group' })).toBeVisible()
    await expect(
      tasks.getByRole('row').filter({ hasText: 'app.billing.send' }).getByRole('cell').nth(1),
    ).toHaveText('Billing')
    // A task in no group has an empty cell, not a placeholder
    await expect(
      tasks.getByRole('row').filter({ hasText: 'app.add' }).getByRole('cell').nth(1),
    ).toHaveText('')
  })

  test('a worker with no registered tasks has no empty table', async ({ page }) => {
    api.setWorkers([worker({ registered_tasks: [] })])
    await openWorker(page)
    await expect(panel(page, 'Registered tasks').getByText('No registered tasks')).toBeVisible()
    await expect(panel(page, 'Registered tasks').getByRole('table')).toHaveCount(0)
  })

  test('recent runs come from the tasks API for this worker only, newest 25', async ({ page }) => {
    const others = createSuccessTask('app.other')
    api.addTask({ ...others, hostname: 'celery@beta' })
    api.addTasks(Array.from({ length: 30 }, (_, i) => run(`app.job_${i}`)))
    const requests: URL[] = []
    page.on('request', (request) => {
      const url = new URL(request.url())
      if (url.pathname === '/api/tasks') requests.push(url)
    })
    await openWorker(page)
    const recent = panel(page, 'Recent executions')
    await expect(recent.getByRole('row')).toHaveCount(26) // header + 25
    await expect(recent.getByRole('link', { name: 'app.other' })).toHaveCount(0)
    const mine = requests.filter((url) => url.searchParams.get('hostname') === HOST)
    expect(mine).toHaveLength(1)
    expect(mine[0].searchParams.get('limit')).toBe('25')
    expect(mine[0].searchParams.get('compact')).toBe('1')
    // No time window: the latest runs, however old
    expect(mine[0].searchParams.has('within')).toBe(false)
  })

  test('a recent run opens the Task page', async ({ page }) => {
    const task = run('app.billing.send')
    api.addTask(task)
    await openWorker(page)
    const row = panel(page, 'Recent executions')
      .getByRole('link')
      .filter({ hasText: 'app.billing.send' })
    await expect(row).toHaveAttribute('href', `/tasks/${task.task_id}`)
    await row.click()
    await expect(page).toHaveURL(new RegExp(`/tasks/${task.task_id}$`))
  })

  test('recent runs show State, Task, Queue, Queue wait, Runtime and Updated', async ({ page }) => {
    api.addTask(run('app.billing.send'))
    await openWorker(page)
    const headers = panel(page, 'Recent executions').getByRole('columnheader')
    await expect(headers).toHaveText(['State', 'Task', 'Queue', 'Queue wait', 'Runtime', 'Updated'])
  })

  test('a recent-runs column nothing has a value for is hidden', async ({ page }) => {
    api.addTask(run('app.billing.send', { queue: null, queue_wait_ms: null }))
    await openWorker(page)
    const recent = panel(page, 'Recent executions')
    await expect(recent.getByRole('columnheader', { name: 'Queue wait' })).toHaveCount(0)
    await expect(recent.getByRole('columnheader', { name: 'Queue', exact: true })).toHaveCount(0)
    await expect(recent.getByRole('columnheader', { name: 'Runtime' })).toBeVisible()
    // The Worker column is the page itself, so it never shows
    await expect(recent.getByRole('columnheader', { name: 'Worker' })).toHaveCount(0)
  })

  test('no runs says so', async ({ page }) => {
    await openWorker(page)
    await expect(
      panel(page, 'Recent executions').getByText('No executions from this worker'),
    ).toBeVisible()
  })

  test('details show the pid, when it started and when it was last seen, and nothing else', async ({
    page,
  }) => {
    await openWorker(page)
    const details = panel(page, 'Details')
    await expect(details.locator('dt')).toHaveText(['PID', 'Started', 'Last seen'])
    await expect(details.locator('dd')).toHaveText(['4242', /3h ago/, /2m ago/])
    for (const absent of ['Pool', 'Queues', 'Active', 'Concurrency']) {
      await expect(details.getByText(absent)).toHaveCount(0)
    }
  })

  test('with several processes on the host, the online one with the latest sighting leads', async ({
    page,
  }) => {
    api.setWorkers([
      worker({
        pid: 1,
        status: 'offline',
        last_seen: minutesAgo(300),
        registered_tasks: ['app.old'],
      }),
      worker({ pid: 2, status: 'online', last_seen: minutesAgo(1), registered_tasks: ['app.new'] }),
      worker({ hostname: 'celery@beta', pid: 3 }),
    ])
    await openWorker(page)
    await expect(page.getByTestId('worker-status')).toHaveText('Online')
    await expect(page.getByRole('note')).toHaveCount(0)
    await expect(panel(page, 'Details').locator('dd').first()).toHaveText('2')
    // The registered tasks are the union of the host's processes, once each
    const tasks = panel(page, 'Registered tasks')
    await expect(tasks.getByRole('link', { name: 'app.new' })).toBeVisible()
    await expect(tasks.getByRole('link', { name: 'app.old' })).toBeVisible()
  })

  test('asks for the refreshed list, never the per-host route, and the badge for the stored one', async ({
    page,
  }) => {
    const requests: string[] = []
    page.on('request', (request) => {
      const url = new URL(request.url())
      if (/\/api\/workers/.test(url.pathname)) requests.push(url.pathname + url.search)
    })
    await openWorker(page)
    await expect.poll(() => requests.includes('/api/workers?refresh=false')).toBe(true)
    expect(requests.filter((r) => r === '/api/workers')).toHaveLength(1)
    expect(requests.filter((r) => r.startsWith('/api/workers/'))).toEqual([])
  })

  test('an unknown hostname says so and links back to the list', async ({ page }) => {
    await openWorker(page, 'celery@nobody')
    await expect(page.getByText('No worker named celery@nobody')).toBeVisible()
    await page.getByRole('link', { name: 'Back to workers' }).click()
    await expect(page).toHaveURL(/\/workers$/)
  })

  test('says so when the workers cannot be loaded', async ({ page }) => {
    await page.route(/\/api\/workers$/, (route) => route.fulfill({ status: 500, body: 'boom' }))
    await page.goto(`/workers/${encodeURIComponent(HOST)}`)
    await expect(page.getByText('Failed to load worker')).toBeVisible({ timeout: 15_000 })
  })

  test('a nodename under a mount prefix opens', async ({ page }) => {
    await page.addInitScript(() => {
      window.__STEMTRACE_BASE__ = '/stemtrace'
    })
    await page.goto(`/stemtrace/workers/${encodeURIComponent(HOST)}`)
    await expect(page.getByRole('heading', { name: HOST, level: 1 })).toBeVisible()
  })

  test('at 400px the page does not scroll sideways', async ({ page }) => {
    await page.setViewportSize({ width: 400, height: 800 })
    api.addTasks(Array.from({ length: 5 }, (_, i) => run(`app.billing.send_${i}`)))
    await openWorker(page)
    await expect(panel(page, 'Recent executions').getByRole('row').nth(1)).toBeVisible()
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(400)
  })
})
