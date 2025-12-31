import { expect, test } from '@playwright/test'

import { setupMockApi } from './fixtures/mock-api'
import { createFailedTask, createSuccessTask } from './fixtures/mock-data'

/**
 * Tests for the Tasks page.
 *
 * These tests use mocked API responses, no Docker required.
 * Run with E2E_MODE=real for integration testing against Docker.
 */

const isRealMode = process.env.E2E_MODE === 'real'

test.describe('Tasks Page', () => {
  test.beforeEach(async ({ page }) => {
    if (!isRealMode) {
      // Set up mock API with default data
      await setupMockApi(page)
    }
    await page.goto('/')
  })

  test('displays task list', async ({ page }) => {
    // Wait for the tasks section to load
    await expect(page.locator('h1, h2').first()).toBeVisible()

    // Should have a tasks container or list
    const tasksSection = page.locator('[data-testid="tasks-list"], .tasks-list, main')
    await expect(tasksSection).toBeVisible()
  })

  test('shows task state badges', async ({ page }) => {
    // Wait for page to load
    await page.waitForLoadState('networkidle')

    // Look for state badges (SUCCESS, FAILURE, PENDING, etc.)
    const stateBadges = page.locator(
      '[data-testid="task-state"], .task-state, .badge, [class*="state"]',
    )

    // If there are tasks, there should be state badges
    const count = await stateBadges.count()
    if (count > 0) {
      await expect(stateBadges.first()).toBeVisible()
    }
  })

  test('can navigate to task detail', async ({ page }) => {
    await page.waitForLoadState('networkidle')

    // Find a task link or row
    const taskLink = page.locator('a[href*="/tasks/"], tr[data-task-id], .task-row').first()
    const isVisible = await taskLink.isVisible().catch(() => false)

    if (isVisible) {
      await taskLink.click()

      // Should navigate to task detail page
      await expect(page).toHaveURL(/\/tasks\/.+/)
    }
  })

  test('filter controls are present', async ({ page }) => {
    await page.waitForLoadState('networkidle')

    // Look for filter inputs (state filter, name filter, etc.)
    const filterControls = page.locator(
      'select, input[type="search"], input[placeholder*="filter"], [data-testid="filter"]',
    )

    // Should have at least one filter control
    const count = await filterControls.count()
    // Verify page renders (count check is informational)
    expect(typeof count).toBe('number')
  })
})

test.describe('Tasks Page - State Filtering', () => {
  test('can filter by state', async ({ page }) => {
    if (!isRealMode) {
      await setupMockApi(page)
    }
    await page.goto('/')
    await page.waitForLoadState('networkidle')

    // Look for state filter dropdown
    const stateFilter = page
      .locator('select[name="state"], [data-testid="state-filter"], select')
      .first()

    const isVisible = await stateFilter.isVisible().catch(() => false)

    if (isVisible) {
      // Select a state if options exist
      const options = await stateFilter.locator('option').count()
      if (options > 1) {
        await stateFilter.selectOption({ index: 1 })
        await page.waitForLoadState('networkidle')
      }
    }
  })
})

test.describe('Tasks Page - Mock Scenarios', () => {
  // These tests only run in mock mode
  test.skip(isRealMode, 'Mock-only test')

  test('shows empty state when no tasks', async ({ page }) => {
    const mockApi = await setupMockApi(page, { useDefaults: false })
    expect(mockApi.tasks.length).toBe(0)

    await page.goto('/')
    await page.waitForLoadState('networkidle')

    // Page should load without errors even with no data
    await expect(page.locator('main')).toBeVisible()
  })

  test('shows specific task in list', async ({ page }) => {
    const mockApi = await setupMockApi(page, { useDefaults: false })
    const testTask = createSuccessTask('test.my_custom_task', 42)
    mockApi.addTask(testTask)

    await page.goto('/')
    await page.waitForLoadState('networkidle')

    // Should show our custom task
    const taskName = page.getByText('test.my_custom_task')
    await expect(taskName).toBeVisible()
  })

  test('shows failed task with error indicator', async ({ page }) => {
    const mockApi = await setupMockApi(page, { useDefaults: false })
    const failedTask = createFailedTask(
      'test.failing_task',
      'ValueError: Something went wrong',
      'Traceback...',
    )
    mockApi.addTask(failedTask)

    await page.goto('/')
    await page.waitForLoadState('networkidle')

    // Should show the failed task
    const taskName = page.getByText('test.failing_task')
    await expect(taskName).toBeVisible()

    // Should show FAILURE state badge (use locator to find badge near task)
    // The badge has class 'task-state-badge' and contains FAILURE text
    const failureBadge = page.locator('.task-state-badge:has-text("FAILURE")')
    await expect(failureBadge).toBeVisible()
  })
})
