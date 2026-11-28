import { expect, type Locator, type Page, test } from '@playwright/test'

import { createFailedTask, createMockEvent } from './fixtures/mock-data'
import { at, openTask, successTask, tokenColour } from './fixtures/task-page'

/** The timing bar of the task page: queue wait and runtime, each present or absent. */

test.skip(process.env.E2E_MODE === 'real', 'Mock-only test')

const timing = (page: Page): Locator => page.getByRole('region', { name: 'Timing' })

/** The value next to a label in the legend under the bar. */
const figure = (section: Locator, label: string): Locator =>
  section.getByText(label, { exact: true }).locator('xpath=following-sibling::dd[1]')

async function width(locator: Locator): Promise<number> {
  const box = await locator.boundingBox()
  return box?.width ?? 0
}

test.describe('TimingBar', () => {
  test('queue wait and the last run are drawn to one scale, with their durations', async ({
    page,
  }) => {
    await openTask(
      page,
      successTask({ queue_wait_ms: 3000, runtime_ms: 1000, total_runtime_ms: 1000 }),
    )
    const section = timing(page)
    const wait = section.getByTestId('timing-queue-wait')
    const runtime = section.getByTestId('timing-runtime')
    await expect(wait).toBeVisible()
    // 3000 ms against 1000 ms: three times as wide (two rounded boxes differ by under a pixel)
    expect((await width(wait)) / (await width(runtime))).toBeCloseTo(3, 1)
    await expect(figure(section, 'Queue wait')).toHaveText('3.0s')
    await expect(figure(section, 'Last run')).toHaveText('1.0s')
    await expect(section.getByRole('img')).toHaveAttribute(
      'aria-label',
      'Queue wait 3.0s, last run 1.0s',
    )
  })

  // `runtime_ms` is the last run only and queue wait is the first attempt's, so their sum is
  // not the task's time; the page never adds them.
  for (const { runs, retries, runtime, total, allRuns } of [
    { runs: 'one run', retries: 0, runtime: 1000, total: 1000, allRuns: null },
    { runs: 'two runs', retries: 1, runtime: 1000, total: 2500, allRuns: '2.5s' },
    { runs: 'three runs', retries: 2, runtime: 1000, total: 6000, allRuns: '6.0s' },
    { runs: 'a retry that took no time', retries: 1, runtime: 1000, total: 1000, allRuns: '1.0s' },
    { runs: 'retries but no total', retries: 2, runtime: 1000, total: null, allRuns: null },
    {
      runs: 'no retry number but more total',
      retries: 0,
      runtime: 1000,
      total: 4000,
      allRuns: '4.0s',
    },
  ]) {
    test(`all runs together are shown only when the task ran more than once: ${runs}`, async ({
      page,
    }) => {
      const task = successTask({ queue_wait_ms: 500, runtime_ms: runtime, total_runtime_ms: total })
      const { task_id, name } = task
      task.events = [
        createMockEvent(task_id, name, 'STARTED', { timestamp: at(1), retries: 0 }),
        createMockEvent(task_id, name, 'SUCCESS', { timestamp: at(2), retries }),
      ]
      await openTask(page, task)
      const section = timing(page)
      await expect(figure(section, 'Last run')).toHaveText('1.0s')
      if (allRuns) await expect(figure(section, 'All runs')).toHaveText(allRuns)
      else await expect(section.getByText('All runs')).toHaveCount(0)
      await expect(section.getByText('Total')).toHaveCount(0)
    })
  }

  test('without a queue wait only the runtime block is drawn, as wide as the bar', async ({
    page,
  }) => {
    await openTask(page, successTask({ queue_wait_ms: null, runtime_ms: 1500 }))
    const section = timing(page)
    await expect(section.getByTestId('timing-queue-wait')).toHaveCount(0)
    await expect(section.getByText('Queue wait')).toHaveCount(0)
    await expect(section.getByText('Total')).toHaveCount(0)
    await expect(section.getByText('1.5s', { exact: true })).toBeVisible()
    const runtime = section.getByTestId('timing-runtime')
    expect(await width(runtime)).toBeCloseTo(await width(section.getByRole('img')), 0)
  })

  test('a task still waiting in the queue shows the queue wait alone', async ({ page }) => {
    await openTask(page, successTask({ state: 'PENDING', queue_wait_ms: 2500, runtime_ms: null }))
    const section = timing(page)
    await expect(section.getByTestId('timing-runtime')).toHaveCount(0)
    await expect(section.getByText('Last run')).toHaveCount(0)
    await expect(section.getByText('2.5s', { exact: true })).toBeVisible()
  })

  test('with neither there is no timing section', async ({ page }) => {
    await openTask(page, successTask({ queue_wait_ms: null, runtime_ms: null }))
    await expect(page.getByRole('heading', { name: 'Timing' })).toHaveCount(0)
  })

  test('the runtime block takes the colour of the state', async ({ page }) => {
    await openTask(page, successTask({ runtime_ms: 1000 }))
    await expect(timing(page).getByTestId('timing-runtime')).toHaveCSS(
      'background-color',
      await tokenColour(page, 'success'),
    )
  })

  test('a failed run is drawn in the failure colour', async ({ page }) => {
    const failed = createFailedTask('app.bad', 'ValueError: boom', 'tb')
    failed.runtime_ms = 500
    await openTask(page, failed)
    await expect(timing(page).getByTestId('timing-runtime')).toHaveCSS(
      'background-color',
      await tokenColour(page, 'failure'),
    )
  })
})
