import { expect, type Page, test } from '@playwright/test'

import { createMockEvent, createSuccessTask, type MockTask } from './fixtures/mock-data'
import { at, openTask } from './fixtures/task-page'

/** The task page as a whole: every section present at once, at desktop and phone width. */

test.skip(process.env.E2E_MODE === 'real', 'Mock-only test')

const LONG = 'x'.repeat(300)

/** A retried, failed task with every optional field, and values too long for a phone. */
function fullTask(): { task: MockTask; child: MockTask } {
  const task = createSuccessTask('billing.invoices.tasks.reminders.send_overdue_invoice_reminder')
  const child = createSuccessTask('billing.invoices.tasks.reminders.record_reminder_sent')
  const { task_id, name } = task
  task.state = 'FAILURE'
  task.children = [child.task_id]
  Object.assign(task, {
    hostname: 'celery@worker-billing-with-a-long-hostname-1.internal.example.com',
    queue: 'invoices',
    routing_key: 'invoices.reminders.overdue',
    task_group: 'Billing',
    tags: ['money', 'nightly'],
    queue_wait_ms: 1800,
    runtime_ms: 30_000,
    exception_type: 'GatewayTimeout',
    eta: at(-60),
  })
  task.events = [
    createMockEvent(task_id, name, 'PENDING', { timestamp: at(0), args: [LONG] }),
    createMockEvent(task_id, name, 'STARTED', {
      timestamp: at(2),
      retries: 0,
      kwargs: { customer: LONG, password: '••••••••' },
    }),
    createMockEvent(task_id, name, 'RETRY', {
      timestamp: at(4),
      retries: 0,
      exception: `ConnectionError: ${LONG}`,
      traceback: `Traceback\n  ${LONG}`,
      output: `WARNING ${LONG}`,
    }),
    createMockEvent(task_id, name, 'STARTED', { timestamp: at(6), retries: 1 }),
    createMockEvent(task_id, name, 'FAILURE', {
      timestamp: at(9),
      retries: 1,
      exception: `GatewayTimeout: ${LONG}`,
      traceback: `Traceback\n  ${LONG}`,
      output: `ERROR ${LONG}`,
    }),
  ]
  task.first_seen = at(0)
  task.last_updated = at(9)
  task.duration_ms = 9000
  return { task, child }
}

async function horizontalOverflow(page: Page): Promise<number> {
  return page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  )
}

test.describe('Task page layout', () => {
  test('at 400px every section stacks and the page has no horizontal scroll', async ({ page }) => {
    await page.setViewportSize({ width: 400, height: 900 })
    const { task, child } = fullTask()
    await openTask(page, task, child)
    for (const name of ['Timing', 'Exception', 'Output', 'Details', 'Arguments', 'Event History']) {
      await expect(page.getByRole('region', { name })).toBeVisible()
    }
    // Open every traceback (a toggle changes its name once clicked, so always take the first)
    const closed = page.getByRole('button', { name: 'Show traceback' })
    while ((await closed.count()) > 0) await closed.first().click()
    // and the older attempt's output: 3 tracebacks and 2 outputs
    await page.getByRole('button', { name: /expand/ }).click()
    await expect(page.locator('pre')).toHaveCount(5)
    expect(await horizontalOverflow(page)).toBe(0)
  })

  test('at 1280px the timing sits left of the details', async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 900 })
    const { task, child } = fullTask()
    await openTask(page, task, child)
    const timing = await page.getByRole('region', { name: 'Timing' }).boundingBox()
    const details = await page.getByRole('region', { name: 'Details' }).boundingBox()
    expect((timing?.x ?? 0) + (timing?.width ?? 0)).toBeLessThanOrEqual(details?.x ?? 0)
    expect(await horizontalOverflow(page)).toBe(0)
  })

  test('at 400px the details are below the timing, in one column', async ({ page }) => {
    await page.setViewportSize({ width: 400, height: 900 })
    const { task, child } = fullTask()
    await openTask(page, task, child)
    const timing = await page.getByRole('region', { name: 'Timing' }).boundingBox()
    const details = await page.getByRole('region', { name: 'Details' }).boundingBox()
    expect(details?.y ?? 0).toBeGreaterThan((timing?.y ?? 0) + (timing?.height ?? 0))
    expect(details?.x).toBe(timing?.x)
  })

  test('no legacy slate or palette colours remain on the page', async ({ page }) => {
    const { task, child } = fullTask()
    await openTask(page, task, child)
    await expect(page.getByRole('region', { name: 'Child Tasks' })).toBeVisible()
    await expect(page.locator('main [class*="slate-"], main [class*="-400"]')).toHaveCount(0)
  })

  test('a task page with no optional data is the header and the history', async ({ page }) => {
    const task = createSuccessTask('app.bare')
    task.events = [createMockEvent(task.task_id, task.name, 'SUCCESS', { timestamp: at(0) })]
    task.first_seen = null
    task.last_updated = null
    task.duration_ms = null
    await openTask(page, task)
    await expect(page.getByRole('region', { name: 'Event History' })).toBeVisible()
    for (const name of ['Timing', 'Details', 'Arguments', 'Result', 'Exception', 'Output']) {
      await expect(page.getByRole('region', { name })).toHaveCount(0)
    }
  })
})
