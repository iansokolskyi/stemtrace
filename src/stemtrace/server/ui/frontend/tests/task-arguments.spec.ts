import { expect, type Locator, type Page, test } from '@playwright/test'

import { createFailedTask, createMockEvent, type MockTask } from './fixtures/mock-data'
import { at, openTask, successTask } from './fixtures/task-page'

/** The arguments, result and exception blocks of the task page. */

test.skip(process.env.E2E_MODE === 'real', 'Mock-only test')

const args = (page: Page): Locator => page.getByRole('region', { name: 'Arguments' })
const result = (page: Page): Locator => page.getByRole('region', { name: 'Result' })
const exception = (page: Page): Locator => page.getByRole('region', { name: 'Exception' })

/** The value shown next to `key` in a key/value list. */
const cellOf = (list: Locator, key: string): Locator =>
  list.locator('dt', { hasText: new RegExp(`^${key}$`) }).locator('xpath=following-sibling::dd[1]')

function withEvents(task: MockTask, build: (id: string, name: string) => MockTask['events']) {
  task.events = build(task.task_id, task.name)
  return task
}

test.describe('Arguments', () => {
  test('args and kwargs are mono key/value lines, kwargs scrubbed as the API sends them', async ({
    page,
  }) => {
    const task = withEvents(successTask(), (id, name) => [
      createMockEvent(id, name, 'PENDING', { timestamp: at(0), args: [7, 'x'] }),
      createMockEvent(id, name, 'STARTED', {
        timestamp: at(1),
        kwargs: { customer: 'c-1', amount: 12.5, password: '••••••••' },
      }),
      createMockEvent(id, name, 'SUCCESS', { timestamp: at(2) }),
    ])
    await openTask(page, task)
    const list = args(page)
    await expect(cellOf(list, 'args')).toHaveText('[7,"x"]')
    await expect(cellOf(list, 'customer')).toHaveText('"c-1"')
    await expect(cellOf(list, 'amount')).toHaveText('12.5')
    await expect(cellOf(list, 'password')).toHaveText('"••••••••"')
    await expect(cellOf(list, 'amount')).toHaveCSS('font-family', /Overpass Mono Variable/)
  })

  test('args alone give one line and no kwargs', async ({ page }) => {
    await openTask(page, successTask())
    await expect(cellOf(args(page), 'args')).toHaveText('[7]')
    await expect(args(page).locator('dt')).toHaveCount(1)
  })

  test('empty args and kwargs give no section', async ({ page }) => {
    const task = withEvents(successTask(), (id, name) => [
      createMockEvent(id, name, 'PENDING', { timestamp: at(0), args: [], kwargs: {} }),
      createMockEvent(id, name, 'SUCCESS', { timestamp: at(2) }),
    ])
    await openTask(page, task)
    await expect(args(page)).toHaveCount(0)
  })

  test('arguments are read from whichever event carries them first', async ({ page }) => {
    const task = withEvents(successTask(), (id, name) => [
      createMockEvent(id, name, 'PENDING', { timestamp: at(0) }),
      createMockEvent(id, name, 'RECEIVED', { timestamp: at(1), kwargs: { late: true } }),
      createMockEvent(id, name, 'STARTED', { timestamp: at(2), kwargs: { later: true } }),
      createMockEvent(id, name, 'SUCCESS', { timestamp: at(3) }),
    ])
    await openTask(page, task)
    await expect(cellOf(args(page), 'late')).toHaveText('true')
    await expect(args(page).locator('dt', { hasText: /^later$/ })).toHaveCount(0)
  })
})

test.describe('Result', () => {
  test('an object result is listed by key', async ({ page }) => {
    await openTask(page, successTask())
    await expect(cellOf(result(page), 'sent')).toHaveText('true')
  })

  test('a scalar result, even a falsy one, is shown', async ({ page }) => {
    const task = withEvents(successTask(), (id, name) => [
      createMockEvent(id, name, 'STARTED', { timestamp: at(1) }),
      createMockEvent(id, name, 'SUCCESS', { timestamp: at(2), result: 0 }),
    ])
    await openTask(page, task)
    await expect(cellOf(result(page), 'result')).toHaveText('0')
  })

  test('a task without a result has no result section', async ({ page }) => {
    const task = withEvents(successTask(), (id, name) => [
      createMockEvent(id, name, 'STARTED', { timestamp: at(1) }),
      createMockEvent(id, name, 'SUCCESS', { timestamp: at(2), result: null }),
    ])
    await openTask(page, task)
    await expect(result(page)).toHaveCount(0)
  })

  test('a failed task has no result section', async ({ page }) => {
    await openTask(page, createFailedTask('app.bad', 'ValueError: boom', 'tb'))
    await expect(result(page)).toHaveCount(0)
  })
})

test.describe('ExceptionBlock', () => {
  const TRACEBACK =
    'Traceback (most recent call last):\n  File "tasks.py", line 4\nValueError: boom'

  test('shows the type and message, with the traceback collapsed until asked for', async ({
    page,
  }) => {
    await openTask(page, createFailedTask('app.bad', 'ValueError: boom', TRACEBACK))
    const block = exception(page)
    await expect(block.getByText('ValueError', { exact: true })).toBeVisible()
    await expect(block.getByText('ValueError: boom', { exact: true })).toBeVisible()
    await expect(block.locator('pre')).toHaveCount(0)

    const toggle = block.getByRole('button', { name: 'Show traceback' })
    await expect(toggle).toHaveAttribute('aria-expanded', 'false')
    await toggle.click()
    await expect(block.locator('pre')).toHaveText(TRACEBACK)
    await expect(block.getByRole('button', { name: 'Hide traceback' })).toHaveAttribute(
      'aria-expanded',
      'true',
    )
    await block.getByRole('button', { name: 'Hide traceback' }).click()
    await expect(block.locator('pre')).toHaveCount(0)
  })

  test('with several failures it shows the final one', async ({ page }) => {
    const task = withEvents(
      createFailedTask('app.flaky', 'ValueError: last', 'tb-last'),
      (id, name) => [
        createMockEvent(id, name, 'STARTED', { timestamp: at(1), retries: 0 }),
        createMockEvent(id, name, 'RETRY', {
          timestamp: at(2),
          retries: 0,
          exception: 'RuntimeError: first',
          traceback: 'tb-first',
        }),
        createMockEvent(id, name, 'STARTED', { timestamp: at(3), retries: 1 }),
        createMockEvent(id, name, 'FAILURE', {
          timestamp: at(4),
          retries: 1,
          exception: 'ValueError: last',
          traceback: 'tb-last',
        }),
      ],
    )
    await openTask(page, task)
    await expect(exception(page).getByText('ValueError: last', { exact: true })).toBeVisible()
    await expect(exception(page).getByText('RuntimeError: first')).toHaveCount(0)
  })

  test('a retry that later succeeded still shows its last exception', async ({ page }) => {
    const task = withEvents(successTask(), (id, name) => [
      createMockEvent(id, name, 'STARTED', { timestamp: at(1), retries: 0 }),
      createMockEvent(id, name, 'RETRY', {
        timestamp: at(2),
        retries: 0,
        exception: 'ConnectionError: reset',
      }),
      createMockEvent(id, name, 'STARTED', { timestamp: at(3), retries: 1 }),
      createMockEvent(id, name, 'SUCCESS', { timestamp: at(4), retries: 1 }),
    ])
    await openTask(page, task)
    await expect(exception(page).getByText('ConnectionError: reset', { exact: true })).toBeVisible()
    // No traceback was recorded, so there is nothing to toggle
    await expect(exception(page).getByRole('button')).toHaveCount(0)
  })

  test('a task that never failed has no exception section', async ({ page }) => {
    await openTask(page, successTask())
    await expect(exception(page)).toHaveCount(0)
  })

  test('without a known exception type only the message is shown', async ({ page }) => {
    const task = createFailedTask('app.bad', 'ValueError: boom', 'tb')
    task.exception_type = null
    await openTask(page, task)
    const heading = exception(page).getByRole('heading', { name: 'Exception' })
    await expect(heading).toBeVisible()
    await expect(exception(page).getByText('ValueError', { exact: true })).toHaveCount(0)
    await expect(exception(page).getByText('ValueError: boom', { exact: true })).toBeVisible()
  })
})
