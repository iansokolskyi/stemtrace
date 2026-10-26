import { expect, type Locator, type Page, test } from '@playwright/test'

import { setupMockApi } from './fixtures/mock-api'
import { createFailedTask } from './fixtures/mock-data'
import { openTask, successTask, tokenColour } from './fixtures/task-page'

/** The timing bar of the task page: queue wait and runtime, each present or absent. */

test.skip(process.env.E2E_MODE === 'real', 'Mock-only test')

const timing = (page: Page): Locator => page.locator('section', { hasText: 'Timing' })

async function width(locator: Locator): Promise<number> {
  const box = await locator.boundingBox()
  return box?.width ?? 0
}

test.describe('TimingBar', () => {
  test('queue wait and runtime are drawn to one scale, with their durations', async ({ page }) => {
    await openTask(page, successTask({ queue_wait_ms: 3000, runtime_ms: 1000 }))
    const section = timing(page)
    const wait = section.getByTestId('timing-queue-wait')
    const runtime = section.getByTestId('timing-runtime')
    await expect(wait).toBeVisible()
    // 3000 ms against 1000 ms: three times as wide (two rounded boxes differ by under a pixel)
    expect((await width(wait)) / (await width(runtime))).toBeCloseTo(3, 1)
    await expect(section.getByText('Queue wait', { exact: true })).toBeVisible()
    await expect(section.getByText('3.0s', { exact: true })).toBeVisible()
    await expect(section.getByText('1.0s', { exact: true })).toBeVisible()
    await expect(section.getByText('Total', { exact: true })).toBeVisible()
    await expect(section.getByText('4.0s', { exact: true })).toBeVisible()
    await expect(section.getByRole('img')).toHaveAttribute(
      'aria-label',
      'Queue wait 3.0s, runtime 1.0s',
    )
  })

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
    await expect(section.getByText('Runtime')).toHaveCount(0)
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

  test('a retry is hatched with the retry token', async ({ page }) => {
    const api = await setupMockApi(page, { useDefaults: false })
    const retried = successTask({ state: 'RETRY', runtime_ms: 800 })
    api.addTask(retried)
    await page.goto(`/tasks/${retried.task_id}`)
    const image = await timing(page)
      .getByTestId('timing-runtime')
      .evaluate((el) => getComputedStyle(el).backgroundImage)
    expect(image).toContain('repeating-linear-gradient(45deg')
    expect(image).toContain(await tokenColour(page, 'retry'))
  })
})
