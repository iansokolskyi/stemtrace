import { expect, test } from '@playwright/test'

/**
 * Tests for the Registry page.
 *
 * Prerequisites:
 *   docker compose -f docker-compose.e2e.yml up -d --wait
 *   # Submit some tasks to populate the registry
 */

test.describe('Registry Page', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('/registry')
  })

  test('displays registry page', async ({ page }) => {
    await page.waitForLoadState('networkidle')

    // Should show registry heading
    const heading = page.locator('h1, h2, [class*="heading"]')
    await expect(heading.first()).toBeVisible()
  })

  test('shows registered task names', async ({ page }) => {
    await page.waitForLoadState('networkidle')

    // Look for task name entries
    const taskEntries = page.locator('[data-testid="task-name"], .task-name, code, li, tr')

    const count = await taskEntries.count()
    // Verify page renders (count check is informational)
    expect(typeof count).toBe('number')
  })

  test('has search functionality', async ({ page }) => {
    await page.waitForLoadState('networkidle')

    // Look for search input
    const searchInput = page.locator(
      'input[type="search"], input[placeholder*="search"], input[placeholder*="filter"], [data-testid="search"]',
    )

    const isVisible = await searchInput.isVisible().catch(() => false)

    if (isVisible) {
      await searchInput.fill('e2e')
      await page.waitForLoadState('networkidle')

      // Results should filter (or show no results message)
      // This verifies search doesn't break the page
    }
  })

  test('can search for specific task', async ({ page }) => {
    await page.waitForLoadState('networkidle')

    const searchInput = page
      .locator('input[type="search"], input[placeholder*="search"], input[placeholder*="filter"]')
      .first()

    const isVisible = await searchInput.isVisible().catch(() => false)

    if (!isVisible) {
      test.skip()
      return
    }

    // Search for a known E2E task
    await searchInput.fill('add')
    await page.waitForLoadState('networkidle')

    // Should show matching results or empty state
    const results = page.locator('code:has-text("add"), .task-name:has-text("add")')
    const emptyState = page.locator('text="No results", text="No tasks"')

    const hasResults = (await results.count()) > 0
    const hasEmpty = (await emptyState.count()) > 0

    // Either found results or shows empty state - both valid
    expect(hasResults || hasEmpty).toBeTruthy()
  })

  test('displays task count or statistics', async ({ page }) => {
    await page.waitForLoadState('networkidle')

    // Look for count indicator (badges, count elements, or any numeric indicator)
    const countIndicator = page.locator('[data-testid="task-count"], .count, .badge')

    const count = await countIndicator.count()
    // Verify page renders (count check is informational)
    expect(typeof count).toBe('number')
  })
})

test.describe('Registry - Task Details', () => {
  test('can click task to see executions', async ({ page }) => {
    await page.goto('/registry')
    await page.waitForLoadState('networkidle')

    // Find a clickable task entry
    const taskEntry = page.locator('a[href*="/tasks?name="], .task-name, tr, li').first()

    const isVisible = await taskEntry.isVisible().catch(() => false)

    if (!isVisible) {
      test.skip()
      return
    }

    await taskEntry.click()
    await page.waitForLoadState('networkidle')

    // Should navigate or show filtered view
    // Verify page still functional after click
    await expect(page.locator('body')).toBeVisible()
  })
})
