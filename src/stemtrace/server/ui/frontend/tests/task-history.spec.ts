import { expect, type Locator, type Page, test } from '@playwright/test'

import { createFailedTask, createMockEvent, type MockTask } from './fixtures/mock-data'
import { at, openTask, successTask, tokenColour } from './fixtures/task-page'

/** The event history and the execution timeline of the task page. */

test.skip(process.env.E2E_MODE === 'real', 'Mock-only test')

const history = (page: Page): Locator => page.getByRole('region', { name: 'Event History' })
const timeline = (page: Page): Locator => page.getByRole('region', { name: 'Execution Timeline' })

/** PENDING, a retry, a second attempt that fails: two failure-like rows. */
function retriedThenFailed(): MockTask {
  const task = createFailedTask('app.flaky', 'ValueError: last', 'tb-last')
  const { task_id, name } = task
  task.events = [
    createMockEvent(task_id, name, 'PENDING', { timestamp: at(0), retries: 0 }),
    createMockEvent(task_id, name, 'STARTED', { timestamp: at(1), retries: 0 }),
    createMockEvent(task_id, name, 'RETRY', {
      timestamp: at(2),
      retries: 0,
      exception: 'RuntimeError: first',
      traceback: 'tb-first',
    }),
    createMockEvent(task_id, name, 'STARTED', { timestamp: at(3), retries: 1 }),
    createMockEvent(task_id, name, 'FAILURE', {
      timestamp: at(5),
      retries: 1,
      exception: 'ValueError: last',
      traceback: 'tb-last',
    }),
  ]
  return task
}

test.describe('EventHistory', () => {
  test('rows keep the raw event states, in time order, with retry numbers and reasons', async ({
    page,
  }) => {
    await openTask(page, retriedThenFailed())
    const rows = history(page).getByTestId('event-row')
    await expect(rows).toHaveCount(5)
    const states = ['PENDING', 'STARTED', 'RETRY', 'STARTED', 'FAILURE']
    for (const [i, state] of states.entries()) {
      await expect(rows.nth(i)).toContainText(state)
      await expect(rows.nth(i).locator('[data-state]')).toHaveAttribute('data-state', state)
    }
    await expect(rows.nth(2)).toContainText('RuntimeError: first')
    await expect(rows.nth(3)).toContainText('retry #1')
    await expect(rows.nth(4)).toContainText('ValueError: last')
  })

  test('only the final failure or retry row is loud', async ({ page }) => {
    await openTask(page, retriedThenFailed())
    const rows = history(page).getByTestId('event-row')
    await expect(history(page).locator('[data-variant="loud"]')).toHaveCount(1)
    await expect(rows.nth(4).locator('[data-variant="loud"]')).toHaveCount(1)
    // The earlier retry is quiet: muted regular text, no filled block
    const earlier = rows.nth(2).locator('[data-state="RETRY"]')
    await expect(earlier).toHaveAttribute('data-variant', 'quiet')
    await expect(earlier).toHaveCSS('color', await tokenColour(page, 'muted'))
  })

  test('a retry that later succeeded is the one loud row', async ({ page }) => {
    const task = successTask()
    const { task_id, name } = task
    task.events = [
      createMockEvent(task_id, name, 'STARTED', { timestamp: at(1), retries: 0 }),
      createMockEvent(task_id, name, 'RETRY', { timestamp: at(2), retries: 0, exception: 'x' }),
      createMockEvent(task_id, name, 'STARTED', { timestamp: at(3), retries: 1 }),
      createMockEvent(task_id, name, 'SUCCESS', { timestamp: at(4), retries: 1 }),
    ]
    await openTask(page, task)
    const rows = history(page).getByTestId('event-row')
    await expect(history(page).locator('[data-variant="loud"]')).toHaveCount(1)
    await expect(rows.nth(1).locator('[data-variant="loud"]')).toHaveCount(1)
  })

  test('a task that never failed has no loud row', async ({ page }) => {
    await openTask(page, successTask())
    await expect(history(page).getByTestId('event-row')).toHaveCount(3)
    await expect(history(page).locator('[data-variant="loud"]')).toHaveCount(0)
  })

  test('a row with a traceback opens and closes it', async ({ page }) => {
    await openTask(page, retriedThenFailed())
    const row = history(page).getByTestId('event-row').nth(2)
    await expect(row.locator('pre')).toHaveCount(0)
    await row.getByRole('button', { name: 'Show traceback' }).click()
    await expect(row.locator('pre')).toHaveText('tb-first')
    await row.getByRole('button', { name: 'Hide traceback' }).click()
    await expect(row.locator('pre')).toHaveCount(0)
  })

  test('rows without a traceback have no toggle', async ({ page }) => {
    await openTask(page, retriedThenFailed())
    const rows = history(page).getByTestId('event-row')
    await expect(rows.nth(0).getByRole('button')).toHaveCount(0)
  })
})

test.describe('ExecutionTimeline', () => {
  test('one segment per event, each with its state and duration, in the state colour', async ({
    page,
  }) => {
    const task = successTask()
    const { task_id, name } = task
    task.events = [
      createMockEvent(task_id, name, 'PENDING', { timestamp: at(0) }),
      createMockEvent(task_id, name, 'STARTED', { timestamp: at(1) }),
      createMockEvent(task_id, name, 'SUCCESS', { timestamp: at(3) }),
    ]
    await openTask(page, task)
    const segments = timeline(page).getByTestId('timeline-segment')
    await expect(segments).toHaveCount(3)
    await expect(segments.nth(1)).toContainText('STARTED')
    await expect(segments.nth(1)).toContainText('2.0s')
    await expect(segments.nth(2).locator('[data-segment-swatch]')).toHaveCSS(
      'background-color',
      await tokenColour(page, 'success'),
    )
    await expect(segments.nth(1).locator('[data-segment-swatch]')).toHaveCSS(
      'background-color',
      await tokenColour(page, 'running'),
    )
  })

  test('durations use the same format as the rest of the page', async ({ page }) => {
    const task = successTask()
    const { task_id, name } = task
    task.events = [
      createMockEvent(task_id, name, 'PENDING', { timestamp: at(0) }),
      createMockEvent(task_id, name, 'STARTED', { timestamp: at(125) }),
      createMockEvent(task_id, name, 'SUCCESS', { timestamp: at(125.4) }),
    ]
    await openTask(page, task)
    const segments = timeline(page).getByTestId('timeline-segment')
    // 125 s is `2m 5s` everywhere, not `2.1m`
    await expect(segments.nth(0)).toContainText('2m 5s')
    await expect(segments.nth(1)).toContainText('400ms')
    await expect(segments.nth(2)).toContainText('0ms')
  })

  test('a failure segment takes the failure colour', async ({ page }) => {
    await openTask(page, retriedThenFailed())
    const last = timeline(page).getByTestId('timeline-segment').last()
    await expect(last.locator('[data-segment-swatch]')).toHaveCSS(
      'background-color',
      await tokenColour(page, 'failure'),
    )
  })

  test('a retry segment is hatched', async ({ page }) => {
    await openTask(page, retriedThenFailed())
    const retry = timeline(page).getByTestId('timeline-segment').nth(2)
    const image = await retry
      .locator('[data-segment-swatch]')
      .evaluate((el) => getComputedStyle(el).backgroundImage)
    expect(image).toContain('repeating-linear-gradient(45deg')
  })

  test('hovering a segment dims the others', async ({ page }) => {
    await openTask(page, successTask())
    const segments = timeline(page).getByTestId('timeline-segment')
    await segments.nth(1).hover()
    await expect(segments.nth(0)).toHaveCSS('opacity', '0.5')
    await expect(segments.nth(1)).toHaveCSS('opacity', '1')
  })

  test('a task with a single event has no timeline', async ({ page }) => {
    const task = successTask()
    task.events = task.events.slice(0, 1)
    await openTask(page, task)
    await expect(timeline(page)).toHaveCount(0)
    await expect(history(page).getByTestId('event-row')).toHaveCount(1)
  })
})
