import { expect, type Page, test } from '@playwright/test'

import { createMockEvent, type MockTask } from './fixtures/mock-data'
import { at, openTask, successTask, tokenColour } from './fixtures/task-page'

/** The output of each attempt, drawn with the Signal box tokens. */

test.skip(process.env.E2E_MODE === 'real', 'Mock-only test')

function taskWithOutput(output: string): MockTask {
  const task = successTask()
  const { task_id, name } = task
  task.events = [
    createMockEvent(task_id, name, 'STARTED', { timestamp: at(1), retries: 0 }),
    createMockEvent(task_id, name, 'SUCCESS', { timestamp: at(2), retries: 0, output }),
  ]
  return task
}

const section = (page: Page) => page.getByTestId('task-output')

test.describe('TaskOutput', () => {
  test('is a titled section with the output in mono on the ground tone', async ({ page }) => {
    await openTask(page, taskWithOutput('hello\nworld'))
    await expect(page.getByRole('region', { name: 'Output' })).toBeVisible()
    const text = section(page).getByTestId('output-text')
    await expect(text).toHaveText('hello\nworld')
    await expect(text).toHaveCSS('font-family', /Overpass Mono Variable/)
    await expect(text).toHaveCSS('background-color', await tokenColour(page, 'bg'))
    await expect(text).toHaveCSS('color', await tokenColour(page, 'ink'))
  })

  test('an attempt header shows the raw state quietly and toggles the output', async ({ page }) => {
    await openTask(page, taskWithOutput('hello'))
    const attempt = section(page).getByTestId('output-attempt')
    const chip = attempt.locator('[data-state]')
    await expect(chip).toHaveAttribute('data-state', 'SUCCESS')
    await expect(chip).toHaveAttribute('data-variant', 'quiet')
    await expect(chip).toContainText('SUCCESS')

    const toggle = attempt.getByRole('button', { name: /collapse/ })
    await expect(toggle).toHaveAttribute('aria-expanded', 'true')
    await toggle.click()
    await expect(attempt.getByTestId('output-text')).toHaveCount(0)
    await expect(attempt.getByRole('button', { name: /expand/ })).toHaveAttribute(
      'aria-expanded',
      'false',
    )
  })

  test('the attempt of a failed run is quiet too: the failure is shown above', async ({ page }) => {
    const task = taskWithOutput('boom')
    task.events[1] = createMockEvent(task.task_id, task.name, 'FAILURE', {
      timestamp: at(2),
      retries: 0,
      exception: 'ValueError: boom',
      output: 'boom',
    })
    await openTask(page, task)
    await expect(section(page).locator('[data-state="FAILURE"]')).toHaveAttribute(
      'data-variant',
      'quiet',
    )
  })

  test('a long line scrolls inside the output, not the page, at 400px', async ({ page }) => {
    await page.setViewportSize({ width: 400, height: 800 })
    await openTask(page, taskWithOutput(`ERROR ${'x'.repeat(400)}`))
    const text = section(page).getByTestId('output-text')
    await expect(text).toBeVisible()
    await expect(text).toHaveCSS('overflow-x', 'auto')
    expect(await text.evaluate((el) => el.scrollWidth > el.clientWidth)).toBe(true)
    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
    )
    expect(overflow).toBe(0)
  })
})
