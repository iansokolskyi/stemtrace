import { expect, type Page, test } from '@playwright/test'

import { setupMockApi } from './fixtures/mock-api'
import { createFailedTask, createSuccessTask, type MockTask } from './fixtures/mock-data'

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
  { theme, width, path }: { theme: Theme; width: number; path: string },
): Promise<void> {
  await page.clock.setFixedTime(new Date('2026-10-17T12:00:00Z'))
  await page.addInitScript((t) => localStorage.setItem('stemtrace-theme', t), theme)
  await page.setViewportSize({ width, height: 900 })
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
      await page.addInitScript((d) => localStorage.setItem('stemtrace-density', d), density)
      await snapshot(page, `executions-${theme}-${density}-1280`, {
        theme,
        width: 1280,
        path: '/executions',
      })
    })
  }
}
