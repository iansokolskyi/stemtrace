import { expect, type Locator, type Page, test } from '@playwright/test'

import { createMockEvent, type MockTask } from './fixtures/mock-data'
import { at, openTask, successTask } from './fixtures/task-page'

/** The facts list of the task page: a row exists only when its value is known. */

test.skip(process.env.E2E_MODE === 'real', 'Mock-only test')

const facts = (page: Page): Locator => page.locator('section', { hasText: 'Details' })

/** The browser's rendering of a timestamp, so the test doesn't depend on the runner's locale. */
async function inBrowser(page: Page, iso: string): Promise<string> {
  return page.evaluate(
    (value) =>
      new Date(value).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'medium' }),
    iso,
  )
}

interface FactCase {
  label: string
  /** Fields that make the row appear, and the text it shows. */
  present: Partial<MockTask>
  value: string
}

const CASES: FactCase[] = [
  { label: 'Group', present: { task_group: 'Billing' }, value: 'Billing' },
  { label: 'Tags', present: { tags: ['money', 'nightly'] }, value: 'money, nightly' },
  { label: 'Worker', present: { hostname: 'celery@worker-1' }, value: 'celery@worker-1' },
  { label: 'Queue', present: { queue: 'invoices' }, value: 'invoices' },
  { label: 'Routing key', present: { routing_key: 'invoices.send' }, value: 'invoices.send' },
  { label: 'Duration', present: { duration_ms: 2000 }, value: '2.0s' },
]

test.describe('Facts', () => {
  for (const { label, present, value } of CASES) {
    test(`${label} shows its value when known`, async ({ page }) => {
      await openTask(page, successTask(present))
      const row = facts(page).locator('dt', { hasText: new RegExp(`^${label}$`) })
      await expect(row).toBeVisible()
      await expect(row.locator('xpath=following-sibling::dd[1]')).toHaveText(value)
    })

    test(`${label} has no row when unknown, and no dash stands in for it`, async ({ page }) => {
      const absent: Record<string, Partial<MockTask>> = {
        Group: { task_group: null },
        Tags: { tags: [] },
        Worker: { hostname: null },
        Queue: { queue: null },
        'Routing key': { routing_key: null },
        Duration: { duration_ms: null },
      }
      await openTask(page, successTask(absent[label]))
      await expect(facts(page).locator('dt', { hasText: new RegExp(`^${label}$`) })).toHaveCount(0)
      // The section itself is there: other facts (the times) are known
      await expect(facts(page).locator('dt', { hasText: /^First seen$/ })).toBeVisible()
      await expect(facts(page).getByText(/^[–—-]$/)).toHaveCount(0)
    })
  }

  test('with nothing known there is no details section', async ({ page }) => {
    await openTask(
      page,
      successTask({ duration_ms: null, first_seen: null, last_updated: null, hostname: null }),
    )
    await expect(page.getByRole('heading', { name: 'Details' })).toHaveCount(0)
  })

  test('First seen, Last update and ETA are shown as dates when known', async ({ page }) => {
    await openTask(page, successTask({ eta: at(-30) }))
    const dd = (label: string) =>
      facts(page)
        .locator('dt', { hasText: new RegExp(`^${label}$`) })
        .locator('xpath=following-sibling::dd[1]')
    await expect(dd('First seen')).toHaveText(await inBrowser(page, at(0)))
    await expect(dd('Last update')).toHaveText(await inBrowser(page, at(2)))
    await expect(dd('ETA')).toHaveText(await inBrowser(page, at(-30)))
  })

  test('ETA has no row without one', async ({ page }) => {
    await openTask(page, successTask({ eta: null }))
    await expect(facts(page).locator('dt', { hasText: /^ETA$/ })).toHaveCount(0)
  })
})

test.describe('Facts, retries', () => {
  function retriedTask(retries: number): MockTask {
    const task = successTask()
    const { task_id, name } = task
    task.events = [
      createMockEvent(task_id, name, 'STARTED', { timestamp: at(1), retries: 0 }),
      createMockEvent(task_id, name, 'RETRY', { timestamp: at(2), retries: 0, exception: 'x' }),
      createMockEvent(task_id, name, 'STARTED', {
        timestamp: at(3),
        retries: Math.min(1, retries),
      }),
      createMockEvent(task_id, name, 'SUCCESS', { timestamp: at(4), retries }),
    ]
    return task
  }

  test('the highest retry number in the events is the retry count', async ({ page }) => {
    await openTask(page, retriedTask(3))
    const row = facts(page).locator('dt', { hasText: /^Retries$/ })
    await expect(row.locator('xpath=following-sibling::dd[1]')).toHaveText('3')
  })

  test('a task that never retried has no retries row', async ({ page }) => {
    await openTask(page, successTask())
    await expect(facts(page).locator('dt', { hasText: /^Retries$/ })).toHaveCount(0)
  })
})
