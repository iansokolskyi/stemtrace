import { expect, type Locator, type Page, test } from '@playwright/test'

import { type MockApiContext, setupMockApi } from './fixtures/mock-api'
import { createAnomaly, createSuccessTask, type MockAnomaly } from './fixtures/mock-data'
import { expectFilledWithToken } from './fixtures/table'

/**
 * The Anomalies page, the sidebar badge, the Executions column and the Task page callout.
 * Mock mode: the fixtures say what the server's `/api/anomalies` and the `anomaly` fields send.
 */

test.skip(process.env.E2E_MODE === 'real', 'Mock-only test')

const GPU_2 = 'celery@media-gpu-2'
const GPU_1 = 'celery@media-gpu-1'
const minutesAgo = (minutes: number): string =>
  new Date(Date.now() - minutes * 60_000).toISOString()

const stuckTask = createSuccessTask('media.tasks.transcode_upload')
stuckTask.state = 'STARTED'
stuckTask.hostname = GPU_2
stuckTask.anomaly = 'stuck'
const lostTask = createSuccessTask('media.tasks.transcode_upload')
lostTask.state = 'STARTED'
lostTask.hostname = GPU_1
lostTask.anomaly = 'interrupted'

const stuck = createAnomaly('stuck', {
  task_id: stuckTask.task_id,
  name: stuckTask.name,
  since: minutesAgo(41),
  hostname: GPU_2,
  elapsed_ms: 41 * 60_000,
  threshold_ms: 66 * 60_000,
  p95_ms: 22 * 60_000,
  runtime_samples: 12,
})
const interrupted = createAnomaly('interrupted', {
  task_id: lostTask.task_id,
  name: lostTask.name,
  since: minutesAgo(38),
  hostname: GPU_1,
  evidence: 'worker_shutdown',
})
const renamed = createAnomaly('unregistered', {
  name: 'billing.tasks.sync_invoices_v2',
  since: minutesAgo(55),
  task_count: 6,
  similar_name: 'billing.tasks.sync_invoices',
})
const plainUnregistered = createAnomaly('unregistered', {
  name: 'emails.tasks.send_digest',
  since: minutesAgo(20),
})
const fleet = createAnomaly('no_workers_online', { since: minutesAgo(90), task_count: 14 })

async function open(
  page: Page,
  anomalies: MockAnomaly[] | null | 'error',
  path = '/anomalies',
): Promise<MockApiContext> {
  await page.routeWebSocket(/\/ws$/, () => {})
  const api = await setupMockApi(page, { useDefaults: false })
  api.addTasks([stuckTask, lostTask])
  api.setAnomalies(anomalies)
  await page.goto(path)
  return api
}

const items = (page: Page): Locator => page.getByTestId('anomaly-item')
const item = (page: Page, kind: string, name?: string): Locator => {
  const byKind = page.locator(`[data-testid="anomaly-item"][data-kind="${kind}"]`)
  return name ? byKind.filter({ hasText: name }) : byKind
}
const badge = (page: Page): Locator =>
  page.getByRole('navigation', { name: 'Sections' }).getByRole('link', { name: /Anomalies/ })

test.describe('Anomalies page', () => {
  test('each kind shows its word, its name and its explanation', async ({ page }) => {
    await open(page, [stuck, interrupted, renamed, plainUnregistered])
    await expect(page.getByRole('heading', { level: 1 })).toHaveText('Anomalies')
    await expect(
      page.getByText('4 active: tasks that are stuck, interrupted or unregistered'),
    ).toBeVisible()
    await expect(items(page)).toHaveCount(4)

    const stuckItem = item(page, 'stuck')
    await expect(stuckItem.locator('[data-anomaly-flag]')).toHaveText('Stuck')
    await expect(stuckItem.getByTestId('anomaly-explanation')).toHaveText(
      'No result seen for 41m on media-gpu-2; the limit for this task is 1h 6m (p95 22m over 12 runs).',
    )
    await expect(stuckItem).toContainText('media.tasks.transcode_upload')
    await expect(stuckItem).toContainText('since')

    await expect(item(page, 'interrupted').locator('[data-anomaly-flag]')).toHaveText('Interrupted')
    await expect(item(page, 'interrupted').getByTestId('anomaly-explanation')).toHaveText(
      'media-gpu-1 shut down while this task was running; no result will arrive.',
    )
    await expect(item(page, 'unregistered').first().locator('[data-anomaly-flag]')).toHaveText(
      'Unregistered',
    )
  })

  test('the flag is an icon block plus a word, filled with the anomaly token', async ({ page }) => {
    await open(page, [stuck])
    const flag = item(page, 'stuck').locator('[data-anomaly-flag]')
    await expect(flag.locator('svg')).toHaveCount(1)
    await expectFilledWithToken(flag.locator('span').first(), '--anomaly')
  })

  test('an unregistered name with a similar name asks if it was renamed', async ({ page }) => {
    await open(page, [renamed, plainUnregistered])
    await expect(
      item(page, 'unregistered', 'sync_invoices_v2').getByTestId('anomaly-explanation'),
    ).toHaveText(
      '6 tasks PENDING; no online worker registers this name (renamed from billing.tasks.sync_invoices?).',
    )
  })

  test('an unregistered name without a similar name has no rename hint', async ({ page }) => {
    await open(page, [renamed, plainUnregistered])
    const explanation = item(page, 'unregistered', 'send_digest').getByTestId('anomaly-explanation')
    await expect(explanation).toHaveText('1 task PENDING; no online worker registers this name.')
    await expect(explanation).not.toContainText('renamed')
  })

  test('the page has no orphaned kind', async ({ page }) => {
    await open(page, [stuck, interrupted, renamed, fleet])
    await expect(items(page)).toHaveCount(3)
    await expect(page.locator('main')).not.toContainText(/orphan/i)
  })

  test('the fleet item is one banner above the list, not a row', async ({ page }) => {
    await open(page, [stuck, renamed, fleet])
    const banner = page.getByTestId('fleet-banner')
    await expect(banner).toHaveCount(1)
    await expect(banner.getByRole('heading')).toHaveText(
      'No workers online: nothing can run the waiting tasks',
    )
    await expect(banner).toContainText('14 tasks waiting and no worker is online to run them.')
    await expect(items(page)).toHaveCount(2)
    await expect(
      page.locator('[data-testid="anomaly-item"][data-kind="no_workers_online"]'),
    ).toHaveCount(0)
    const bannerBox = await banner.boundingBox()
    const firstBox = await items(page).first().boundingBox()
    expect(bannerBox?.y).toBeLessThan(firstBox?.y ?? 0)
    // The banner counts as one of the active anomalies
    await expect(page.getByText('3 active')).toBeVisible()
  })

  test('with only the fleet item there is a banner and no empty state', async ({ page }) => {
    await open(page, [fleet])
    await expect(page.getByTestId('fleet-banner')).toBeVisible()
    await expect(page.getByTestId('anomalies-empty')).toHaveCount(0)
    await expect(items(page)).toHaveCount(0)
  })

  test('no anomalies shows the empty state', async ({ page }) => {
    await open(page, [])
    const empty = page.getByTestId('anomalies-empty')
    await expect(empty).toHaveText('All clear.')
    await expect(empty.locator('svg')).toHaveCount(1)
    await expect(items(page)).toHaveCount(0)
    await expect(page.getByText('0 active')).toBeVisible()
  })

  test('a failing endpoint says so instead of showing all clear', async ({ page }) => {
    await open(page, 'error')
    await expect(page.getByText('Failed to load anomalies')).toBeVisible()
    await expect(page.getByTestId('anomalies-empty')).toHaveCount(0)
  })
})

test.describe('Anomalies links', () => {
  test('Open task lands on the task page', async ({ page }) => {
    await open(page, [stuck])
    await item(page, 'stuck').getByRole('link', { name: 'Open task' }).click()
    await expect(page).toHaveURL(new RegExp(`/tasks/${stuckTask.task_id}$`))
  })

  test('the worker link lands on that worker page', async ({ page }) => {
    await open(page, [stuck])
    await item(page, 'stuck').getByRole('link', { name: 'Worker media-gpu-2' }).click()
    await expect(page).toHaveURL(/\/workers\/celery(@|%40)media-gpu-2$/)
  })

  test('All runs lands on Executions filtered to the exact name', async ({ page }) => {
    await open(page, [stuck])
    await item(page, 'stuck').getByRole('link', { name: 'All runs' }).click()
    await expect(page).toHaveURL(/\/executions\?.*name_exact=media\.tasks\.transcode_upload/)
  })

  test('an unregistered name links to the Registry and its runs, and has no task link', async ({
    page,
  }) => {
    await open(page, [renamed])
    const row = item(page, 'unregistered')
    await expect(row.getByRole('link', { name: 'Open task' })).toHaveCount(0)
    await expect(row.getByRole('link', { name: 'All runs' })).toHaveAttribute(
      'href',
      /name_exact=billing\.tasks\.sync_invoices_v2/,
    )
    await row.getByRole('link', { name: 'Registry' }).click()
    await expect(page).toHaveURL(/\/registry$/)
  })

  test('the fleet banner links to Workers and to the waiting tasks', async ({ page }) => {
    await open(page, [fleet])
    const banner = page.getByTestId('fleet-banner')
    await expect(banner.getByRole('link', { name: 'Waiting tasks' })).toHaveAttribute(
      'href',
      /state=PENDING/,
    )
    await banner.getByRole('link', { name: 'Workers' }).click()
    await expect(page).toHaveURL(/\/workers$/)
  })

  test('the keyboard reaches a link and Enter follows it', async ({ page }) => {
    await open(page, [stuck])
    await item(page, 'stuck').getByRole('link', { name: 'Open task' }).focus()
    await page.keyboard.press('Enter')
    await expect(page).toHaveURL(new RegExp(`/tasks/${stuckTask.task_id}$`))
  })
})

test.describe('Anomalies badge', () => {
  test('counts every item, the fleet item included', async ({ page }) => {
    await open(page, [stuck, interrupted, renamed, fleet], '/flows')
    await expect(badge(page)).toHaveText('Anomalies4')
  })

  test('is absent with no anomalies', async ({ page }) => {
    await open(page, [], '/flows')
    await expect(badge(page)).toBeVisible()
    await expect(badge(page)).toHaveText('Anomalies')
  })

  test('is absent when the server has no anomalies endpoint', async ({ page }) => {
    await open(page, null, '/flows')
    await expect(badge(page)).toHaveText('Anomalies')
  })

  test('the nav link opens the page and is marked current', async ({ page }) => {
    await open(page, [stuck], '/flows')
    await badge(page).click()
    await expect(page).toHaveURL(/\/anomalies$/)
    await expect(badge(page)).toHaveAttribute('aria-current', 'page')
  })
})

test.describe('Executions anomaly column', () => {
  test('shows the flag word on a flagged row and tints it', async ({ page }) => {
    await open(page, [stuck], '/executions')
    const row = page.getByRole('link', { name: /transcode_upload/ }).first()
    await expect(page.locator('[data-column-header="anomaly"]')).toBeVisible()
    await expect(row.locator('[data-anomaly-flag="stuck"]')).toHaveText('Stuck')
  })

  test('a row without an anomaly has an empty cell', async ({ page }) => {
    const plain = createSuccessTask('billing.invoices.send_receipt')
    const api = await open(page, [], '/executions')
    api.addTask(plain)
    await page.reload()
    const cells = page.locator('[data-cell="anomaly"]')
    await expect(cells).toHaveCount(3)
    const texts = await cells.allTextContents()
    expect(texts.sort()).toEqual(['', 'Interrupted', 'Stuck'])
  })

  test('with no anomaly anywhere the column is left out', async ({ page }) => {
    await page.routeWebSocket(/\/ws$/, () => {})
    const api = await setupMockApi(page, { useDefaults: false })
    api.addTask(createSuccessTask('billing.invoices.send_receipt'))
    await page.goto('/executions')
    await expect(page.locator('[data-column-header="state"]')).toBeVisible()
    await expect(page.locator('[data-column-header="anomaly"]')).toHaveCount(0)
  })
})

test.describe('Task page callout', () => {
  test('a stuck task shows the callout, the flag in the header and a worker link', async ({
    page,
  }) => {
    await open(page, [stuck], `/tasks/${stuckTask.task_id}`)
    const callout = page.getByTestId('anomaly-callout')
    await expect(callout.getByRole('heading')).toHaveText('Stuck: running far longer than usual')
    await expect(callout).toContainText(
      'No result seen for 41m on media-gpu-2; the limit for this task is 1h 6m (p95 22m over 12 runs).',
    )
    await expect(callout).toContainText('Since')
    await expect(page.getByTestId('task-header').locator('[data-anomaly-flag="stuck"]')).toHaveText(
      'Stuck',
    )
    await callout.getByRole('link', { name: 'Worker media-gpu-2' }).click()
    await expect(page).toHaveURL(/\/workers\/celery(@|%40)media-gpu-2$/)
  })

  test('an interrupted task names the stopped worker', async ({ page }) => {
    await open(page, [interrupted], `/tasks/${lostTask.task_id}`)
    const callout = page.getByTestId('anomaly-callout')
    await expect(callout.getByRole('heading')).toHaveText(
      'Interrupted: the worker went away mid-task',
    )
    await expect(callout).toContainText('media-gpu-1 shut down while this task was running')
  })

  test('an unregistered task links to the Registry', async ({ page }) => {
    const pending = createSuccessTask('billing.tasks.sync_invoices_v2')
    pending.state = 'PENDING'
    pending.anomaly = 'unregistered'
    const api = await open(page, [renamed], '/anomalies')
    api.addTask(pending)
    api.setAnomalies([{ ...renamed, task_id: pending.task_id }])
    await page.goto(`/tasks/${pending.task_id}`)
    const callout = page.getByTestId('anomaly-callout')
    await expect(callout.getByRole('heading')).toHaveText('Unregistered: no worker knows this task')
    await expect(callout).toContainText('renamed from billing.tasks.sync_invoices?')
    await callout.getByRole('link', { name: 'Registry' }).click()
    await expect(page).toHaveURL(/\/registry$/)
  })

  test('a task without an anomaly has no callout', async ({ page }) => {
    const plain = createSuccessTask('billing.invoices.send_receipt')
    const api = await open(page, [], '/anomalies')
    api.addTask(plain)
    await page.goto(`/tasks/${plain.task_id}`)
    await expect(page.getByRole('heading', { level: 1 })).toBeVisible()
    await expect(page.getByTestId('anomaly-callout')).toHaveCount(0)
    await expect(page.locator('[data-anomaly-flag]')).toHaveCount(0)
  })
})
