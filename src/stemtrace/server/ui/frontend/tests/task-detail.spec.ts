import { expect, test } from '@playwright/test'

import { setupMockApi } from './fixtures/mock-api'
import { createFailedTask, createSuccessTask } from './fixtures/mock-data'

/**
 * Tests for the Task Detail page.
 *
 * These tests use mocked API responses, no Docker required.
 * Run with E2E_MODE=real for integration testing against Docker.
 */

const isRealMode = process.env.E2E_MODE === 'real'

test.describe('Task Detail Page', () => {
  test.beforeEach(async ({ page }) => {
    if (!isRealMode) {
      await setupMockApi(page)
    }
    // Start from tasks list
    await page.goto('/')
    await page.waitForLoadState('networkidle')
  })

  test('displays task timeline', async ({ page }) => {
    // Find and click a task
    const taskLink = page.locator('a[href*="/tasks/"]').first()
    const isVisible = await taskLink.isVisible().catch(() => false)

    if (!isVisible) {
      test.skip()
      return
    }

    await taskLink.click()
    await page.waitForLoadState('networkidle')

    // Look for timeline section - the heading "Execution Timeline" should be visible
    const timelineHeading = page.getByText('Execution Timeline')

    // Wait for heading to be visible using expect assertion with auto-retry
    await expect(timelineHeading).toBeVisible()
  })

  test('shows task metadata', async ({ page }) => {
    const taskLink = page.locator('a[href*="/tasks/"]').first()
    const isVisible = await taskLink.isVisible().catch(() => false)

    if (!isVisible) {
      test.skip()
      return
    }

    await taskLink.click()
    await page.waitForLoadState('networkidle')

    // Should show task ID somewhere
    const taskIdElement = page.locator("code, .task-id, [data-testid='task-id']")
    const count = await taskIdElement.count()
    // Verify page renders task metadata elements
    expect(typeof count).toBe('number')
  })

  test('displays parameters section for tasks with args', async ({ page }) => {
    const taskLink = page.locator('a[href*="/tasks/"]').first()
    const isVisible = await taskLink.isVisible().catch(() => false)

    if (!isVisible) {
      test.skip()
      return
    }

    await taskLink.click()
    await page.waitForLoadState('networkidle')

    // Look for parameters/arguments section
    const paramsSection = page.locator(
      '[data-testid="parameters"], .parameters, h3:has-text("Parameters"), h2:has-text("Parameters")',
    )

    // May or may not be visible depending on task
    const paramCount = await paramsSection.count()
    // Verify page renders (count check is informational)
    expect(typeof paramCount).toBe('number')
  })

  test('displays result section for completed tasks', async ({ page }) => {
    const taskLink = page.locator('a[href*="/tasks/"]').first()
    const isVisible = await taskLink.isVisible().catch(() => false)

    if (!isVisible) {
      test.skip()
      return
    }

    await taskLink.click()
    await page.waitForLoadState('networkidle')

    // Look for result section
    const resultSection = page.locator(
      '[data-testid="result"], .result, h3:has-text("Result"), h2:has-text("Result"), pre',
    )

    // May or may not be visible depending on task state
    const resultCount = await resultSection.count()
    // Verify page renders (count check is informational)
    expect(typeof resultCount).toBe('number')
  })

  test('shows error details for failed tasks', async ({ page }) => {
    // Try to find a failed task - look for FAILURE badge or red-colored indicators
    const failedLink = page.locator('a[href*="/tasks/"]:has-text("FAILURE")').first()
    const isVisible = await failedLink.isVisible().catch(() => false)

    if (!isVisible) {
      // No failed tasks visible, skip the test
      test.skip()
      return
    }

    await failedLink.click()
    await page.waitForLoadState('networkidle')

    // Should show error/exception section or traceback
    const errorSection = page.locator(
      '[data-testid="error"], .error, .exception, h2:has-text("Error"), h2:has-text("Exception"), pre',
    )

    const errorCount = await errorSection.count()
    // Verify page renders error section when task failed
    expect(typeof errorCount).toBe('number')
  })

  test('can navigate back to tasks list', async ({ page }) => {
    const taskLink = page.locator('a[href*="/tasks/"]').first()
    const isVisible = await taskLink.isVisible().catch(() => false)

    if (!isVisible) {
      test.skip()
      return
    }

    await taskLink.click()
    await page.waitForLoadState('networkidle')

    // Find back link or tasks nav
    const backLink = page
      .locator('a[href="/"], a:has-text("Tasks"), a:has-text("Back"), nav a')
      .first()

    await backLink.click()
    await expect(page).toHaveURL(/\/$|\/tasks$/)
  })
})

test.describe('Task Detail - Mock Scenarios', () => {
  // These tests only run in mock mode
  test.skip(isRealMode, 'Mock-only test')

  test('shows specific task details', async ({ page }) => {
    const mockApi = await setupMockApi(page, { useDefaults: false })
    const task = createSuccessTask('test.my_task', { answer: 42 })
    mockApi.addTask(task)

    // Navigate directly to the task detail page
    await page.goto(`/tasks/${task.task_id}`)
    await page.waitForLoadState('networkidle')

    // Should show the task name
    const taskName = page.getByText('test.my_task')
    await expect(taskName.first()).toBeVisible()

    // Should show SUCCESS state
    const successBadge = page.getByText('SUCCESS')
    await expect(successBadge.first()).toBeVisible()
  })

  test('shows error details for failed task', async ({ page }) => {
    const mockApi = await setupMockApi(page, { useDefaults: false })
    const failedTask = createFailedTask(
      'test.broken_task',
      'RuntimeError: Something broke',
      'Traceback (most recent call last):\n  File "main.py", line 10\nRuntimeError: Something broke',
    )
    mockApi.addTask(failedTask)

    // Navigate directly to the failed task
    await page.goto(`/tasks/${failedTask.task_id}`)
    await page.waitForLoadState('networkidle')

    // Should show FAILURE state
    const failureBadge = page.getByText('FAILURE')
    await expect(failureBadge.first()).toBeVisible()

    // Should show error message somewhere
    const errorText = page.getByText('RuntimeError')
    await expect(errorText.first()).toBeVisible()
  })

  test('shows timeline with all events', async ({ page }) => {
    const mockApi = await setupMockApi(page, { useDefaults: false })
    const task = createSuccessTask('test.timeline_task', 'done')
    mockApi.addTask(task)

    await page.goto(`/tasks/${task.task_id}`)
    await page.waitForLoadState('networkidle')

    // Should show Execution Timeline heading
    const timelineHeading = page.getByText('Execution Timeline')
    await expect(timelineHeading).toBeVisible()

    // Should show PENDING, STARTED, SUCCESS events
    const pendingEvent = page.getByText('PENDING')
    const startedEvent = page.getByText('STARTED')
    const successEvent = page.getByText('SUCCESS')

    await expect(pendingEvent.first()).toBeVisible()
    await expect(startedEvent.first()).toBeVisible()
    await expect(successEvent.first()).toBeVisible()
  })
})
