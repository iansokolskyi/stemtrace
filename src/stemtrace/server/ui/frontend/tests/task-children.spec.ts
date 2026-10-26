import { expect, test } from '@playwright/test'

import { setupMockApi } from './fixtures/mock-api'
import { createFailedTask, createSuccessTask } from './fixtures/mock-data'
import { openTask } from './fixtures/task-page'

/** The child task list of the task page: a quiet list of links to the tasks a task started. */

test.skip(process.env.E2E_MODE === 'real', 'Mock-only test')

test.describe('ChildTasks', () => {
  test('lists each child with its state, as a link to its page', async ({ page }) => {
    const root = createSuccessTask('app.flow.start')
    const ok = createSuccessTask('app.billing.send')
    const bad = createFailedTask('app.email.send', 'ValueError: boom', 'tb')
    for (const child of [ok, bad]) {
      child.parent_id = root.task_id
      root.children.push(child.task_id)
    }
    await openTask(page, root, ok, bad)

    const region = page.getByRole('region', { name: 'Child Tasks' })
    await expect(region.getByRole('link')).toHaveCount(2)
    await expect(region.getByText('2', { exact: true })).toBeVisible()

    const okLink = region.getByRole('link', { name: /billing\.send/ })
    await expect(okLink).toHaveAttribute('href', `/tasks/${ok.task_id}`)
    await expect(okLink).toContainText(ok.task_id.slice(0, 8))
    await expect(okLink.locator('[data-state]')).toHaveAttribute('data-state', 'SUCCESS')
    await expect(okLink.locator('[data-state]')).toHaveAttribute('data-variant', 'quiet')

    const badLink = region.getByRole('link', { name: /email\.send/ })
    await expect(badLink.locator('[data-state]')).toHaveAttribute('data-variant', 'loud')
    // The full name stays on hover
    await expect(okLink.getByTitle('app.billing.send')).toHaveCount(1)
  })

  test('opens the child when its row is clicked', async ({ page }) => {
    const root = createSuccessTask('app.flow.start')
    const child = createSuccessTask('app.flow.finish')
    root.children.push(child.task_id)
    await openTask(page, root, child)

    await page.getByRole('region', { name: 'Child Tasks' }).getByRole('link').click()
    await expect(page).toHaveURL(new RegExp(`/tasks/${child.task_id}$`))
    await expect(page.getByRole('heading', { level: 1 })).toHaveText('finish')
  })

  test('a task without children has no list', async ({ page }) => {
    await openTask(page, createSuccessTask('app.flow.start'))
    await expect(page.getByRole('region', { name: 'Child Tasks' })).toHaveCount(0)
  })

  test('the page of a missing task says so and links back', async ({ page }) => {
    await setupMockApi(page, { useDefaults: false })
    await page.goto('/tasks/no-such-task')
    // The query retries a failed request (1 s, 2 s, 4 s) before it gives up
    await expect(page.getByText('Failed to load task')).toBeVisible({ timeout: 15_000 })
    await expect(page.getByRole('link', { name: 'Back to executions' })).toHaveAttribute(
      'href',
      '/executions',
    )
  })
})
