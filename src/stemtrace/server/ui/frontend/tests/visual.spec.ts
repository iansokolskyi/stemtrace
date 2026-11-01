import { expect, type Page, test } from '@playwright/test'

import { flowListRoots } from './fixtures/flows-list'
import { setupMockApi } from './fixtures/mock-api'
import {
  createFailedTask,
  createMockEvent,
  createSuccessTask,
  type MockTask,
} from './fixtures/mock-data'

/**
 * Visual snapshots of the Signal box frame. Runs only in the `visual` project (PW_VISUAL=1),
 * inside the pinned Playwright image, so font rendering matches the stored baselines:
 *   npm run test:visual          # compare
 *   npm run test:visual:update   # regenerate after an intended change, then review each PNG
 */

type Theme = 'light' | 'dark'

/** Fixed clock, stored theme, viewport, fonts loaded, network idle; then compare. */
async function snapshot(
  page: Page,
  name: string,
  {
    theme,
    width,
    height = 900,
    path,
  }: { theme: Theme; width: number; height?: number; path: string },
): Promise<void> {
  await page.clock.setFixedTime(new Date('2026-10-17T12:00:00Z'))
  await page.addInitScript((t) => localStorage.setItem('stemtrace-theme', t), theme)
  await page.setViewportSize({ width, height })
  await page.goto(path)
  await page.waitForLoadState('networkidle')
  await page.evaluate(() => document.fonts.ready)
  await expect(page).toHaveScreenshot(`${name}.png`, { fullPage: true })
}

for (const theme of ['light', 'dark'] as const) {
  for (const width of [1280, 400]) {
    test(`frame on workers, ${theme}, ${width}px`, async ({ page }) => {
      await page.routeWebSocket(/\/ws$/, () => {})
      await setupMockApi(page)
      await snapshot(page, `frame-${theme}-${width}`, { theme, width, path: '/workers' })
    })
  }
}

test('drawer open at 400px, dark', async ({ page }) => {
  await page.routeWebSocket(/\/ws$/, () => {})
  await setupMockApi(page)
  await page.addInitScript(() => localStorage.setItem('stemtrace-theme', 'dark'))
  await page.setViewportSize({ width: 400, height: 900 })
  await page.clock.setFixedTime(new Date('2026-10-17T12:00:00Z'))
  await page.goto('/workers')
  await page.getByRole('button', { name: 'Open navigation' }).click()
  await expect(page.getByRole('complementary', { name: 'Primary navigation' })).toBeInViewport({
    ratio: 1,
  })
  await page.evaluate(() => document.fonts.ready)
  await expect(page).toHaveScreenshot('drawer-dark-400.png')
})

/**
 * A fixed set of runs for the Executions snapshots: every state, with and without the optional
 * fields, listed newest first against the fixed clock (2026-10-17T12:00:00Z).
 */
function executionRuns(): MockTask[] {
  const minutesAgo = (minutes: number) =>
    new Date(Date.parse('2026-10-17T12:00:00Z') - minutes * 60_000).toISOString()
  const run = (
    task: MockTask,
    minutes: number,
    fields: Partial<MockTask> & { state?: string },
  ): MockTask => Object.assign(task, { last_updated: minutesAgo(minutes) }, fields)
  const group = 'Billing'
  const host = 'celery@worker-billing-1'
  return [
    run(createSuccessTask('billing.invoices.send_receipt'), 0.2, {
      task_group: group,
      hostname: host,
      queue: 'invoices',
      queue_wait_ms: 42,
      runtime_ms: 380,
    }),
    run(
      createFailedTask('billing.payments.capture_payment', 'GatewayTimeout: no answer', 'tb'),
      2,
      {
        task_group: group,
        hostname: host,
        queue: 'payments',
        queue_wait_ms: 1800,
        runtime_ms: 30_000,
      },
    ),
    run(createSuccessTask('billing.payments.capture_payment'), 3, {
      state: 'RETRY',
      task_group: group,
      hostname: 'celery@worker-billing-2',
      queue: 'payments',
      queue_wait_ms: 90,
      runtime_ms: 4200,
    }),
    run(createSuccessTask('search.index.rebuild_product_index_for_catalog_import'), 5, {
      state: 'STARTED',
      task_group: 'Search',
      hostname: 'celery@worker-search-1',
      queue: 'index',
      queue_wait_ms: 15,
    }),
    run(createSuccessTask('emails.send_digest'), 9, {
      state: 'PENDING',
      task_group: 'Email',
      queue: 'default',
    }),
    run(createSuccessTask('emails.send_digest'), 14, {
      task_group: 'Email',
      hostname: 'celery@worker-email-1',
      queue: 'default',
      queue_wait_ms: 130_000,
      runtime_ms: 125_000,
    }),
    run(createSuccessTask('app.cleanup'), 90, { hostname: host, runtime_ms: 12 }),
    run(createSuccessTask('billing.invoices.send_receipt'), 200, {
      task_group: group,
      hostname: host,
      queue: 'invoices',
      queue_wait_ms: 55,
      runtime_ms: 410,
    }),
  ]
}

for (const theme of ['light', 'dark'] as const) {
  for (const density of ['comfortable', 'compact'] as const) {
    test(`executions, ${theme}, ${density}, 1280px`, async ({ page }) => {
      await page.routeWebSocket(/\/ws$/, () => {})
      const api = await setupMockApi(page, { useDefaults: false })
      api.addTasks(executionRuns())
      api.setGroups([
        { name: 'Billing', source: 'code', tags: ['money'], color: null, task_names: [] },
        { name: 'Email', source: 'code', tags: [], color: null, task_names: [] },
        { name: 'Search', source: 'auto', tags: [], color: null, task_names: [] },
      ])
      await page.addInitScript((d) => localStorage.setItem('stemtrace-density', d), density)
      await snapshot(page, `executions-${theme}-${density}-1280`, {
        theme,
        width: 1280,
        // A time chip that doesn't change which rows the mock lists
        path: '/executions?from_date=2026-10-17&to_date=2026-10-17',
      })
    })
  }
}

/**
 * Two task pages with every field filled and fixed IDs and times (the pages show both), against
 * the fixed clock: one that failed after a retry, one that is retrying now.
 */
function taskPageRuns(): { failed: MockTask; retrying: MockTask; child: MockTask } {
  const at = (seconds: number) =>
    new Date(Date.parse('2026-10-17T11:58:00Z') + seconds * 1000).toISOString()
  const common = {
    hostname: 'celery@worker-billing-1',
    queue: 'payments',
    routing_key: 'payments.capture',
    task_group: 'Billing',
    tags: ['money'],
  }

  const failed = createFailedTask(
    'billing.payments.capture_payment',
    'GatewayTimeout: no answer from gateway after 30s',
    'Traceback (most recent call last):\n  File "billing/payments.py", line 88, in capture_payment\n    gateway.charge(order)\nGatewayTimeout: no answer from gateway after 30s',
  )
  failed.task_id = 'f41ed000-0000-4000-8000-000000000001'
  const { task_id, name } = failed
  const child = createSuccessTask('billing.payments.notify_customer')
  child.task_id = 'c41ed000-0000-4000-8000-000000000002'
  child.events = []
  Object.assign(failed, common, {
    root_id: task_id,
    children: [child.task_id],
    queue_wait_ms: 1800,
    // Two runs of 30 s each: the API reports the last run and all of them
    runtime_ms: 30_000,
    total_runtime_ms: 60_000,
    first_seen: at(0),
    last_updated: at(70),
    duration_ms: 70_000,
    eta: at(-5),
  })
  failed.events = [
    createMockEvent(task_id, name, 'PENDING', { timestamp: at(0), root_id: task_id, args: [4821] }),
    createMockEvent(task_id, name, 'STARTED', {
      timestamp: at(2),
      root_id: task_id,
      retries: 0,
      kwargs: { currency: 'EUR', api_key: '••••••••' },
    }),
    createMockEvent(task_id, name, 'RETRY', {
      timestamp: at(32),
      root_id: task_id,
      retries: 0,
      exception: 'GatewayTimeout: no answer from gateway after 30s',
      traceback: 'Traceback ...',
      output: 'INFO payments.capture attempt 1\nWARNING gateway slow',
    }),
    createMockEvent(task_id, name, 'STARTED', { timestamp: at(40), root_id: task_id, retries: 1 }),
    createMockEvent(task_id, name, 'FAILURE', {
      timestamp: at(70),
      root_id: task_id,
      retries: 1,
      exception: 'GatewayTimeout: no answer from gateway after 30s',
      traceback:
        'Traceback (most recent call last):\n  File "billing/payments.py", line 88, in capture_payment\n    gateway.charge(order)\nGatewayTimeout: no answer from gateway after 30s',
      output: 'INFO payments.capture attempt 2\nERROR gateway timed out, giving up',
    }),
  ]

  const retrying = createSuccessTask('emails.send_digest')
  retrying.task_id = 'e41ed000-0000-4000-8000-000000000003'
  const rid = retrying.task_id
  Object.assign(retrying, common, {
    state: 'RETRY',
    root_id: rid,
    hostname: 'celery@worker-email-1',
    queue: 'default',
    routing_key: 'default',
    task_group: 'Email',
    tags: [],
    queue_wait_ms: 130_000,
    // A task that is retrying has not finished, so the API has no runtime yet (only its queue wait)
    runtime_ms: null,
    total_runtime_ms: null,
    first_seen: at(0),
    last_updated: at(140),
    duration_ms: 140_000,
  })
  retrying.events = [
    createMockEvent(rid, retrying.name, 'PENDING', { timestamp: at(0), root_id: rid }),
    createMockEvent(rid, retrying.name, 'STARTED', {
      timestamp: at(130),
      root_id: rid,
      retries: 0,
      args: ['weekly'],
    }),
    createMockEvent(rid, retrying.name, 'RETRY', {
      timestamp: at(140),
      root_id: rid,
      retries: 0,
      exception: 'SMTPServerDisconnected: connection unexpectedly closed',
      traceback: 'Traceback ...',
      output: 'INFO emails.digest rendering 1204 recipients\nWARNING smtp reconnecting',
    }),
  ]
  return { failed, retrying, child }
}

for (const theme of ['light', 'dark'] as const) {
  for (const kind of ['failed', 'retrying'] as const) {
    test(`task page, ${kind}, ${theme}, 1280px`, async ({ page }) => {
      await page.routeWebSocket(/\/ws$/, () => {})
      const api = await setupMockApi(page, { useDefaults: false })
      const runs = taskPageRuns()
      api.addTasks([runs[kind], runs.child])
      await snapshot(page, `task-${kind}-${theme}-1280`, {
        theme,
        width: 1280,
        // Taller than the page, so the dev-only router badge at the viewport's corner sits below it
        height: 1240,
        path: `/tasks/${runs[kind].task_id}`,
      })
    })
  }
}

// The Flows list: every kind of row (failed chord with a callback that did not run, success,
// running, and one without a summary), against the fixed clock.
for (const theme of ['light', 'dark'] as const) {
  test(`flows, ${theme}, 1280px`, async ({ page }) => {
    await page.routeWebSocket(/\/ws$/, () => {})
    const api = await setupMockApi(page, { useDefaults: false })
    api.addTasks(flowListRoots())
    await snapshot(page, `flows-${theme}-1280`, { theme, width: 1280, path: '/flows' })
  })
}
