import { expect, type Page, test } from '@playwright/test'

import { setupMockApi } from './fixtures/mock-api'

/**
 * Tests for the Workers page.
 *
 * These tests use mocked API responses, no Docker required.
 * Run with E2E_MODE=real for integration testing against Docker.
 */

const isRealMode = process.env.E2E_MODE === 'real'

/**
 * Navigate (or reload) and wait until the page has fetched its workers list.
 *
 * The app keeps retrying its WebSocket (no backend in mock mode), so
 * `networkidle` isn't a reliable "page is ready" signal; the API response is.
 */
async function loadWorkersPage(page: Page, navigate: () => Promise<unknown>): Promise<void> {
  const workersResponse = page.waitForResponse((response) =>
    /\/api\/workers(\?|$)/.test(new URL(response.url()).pathname),
  )
  await navigate()
  await workersResponse
}

test.describe('Workers Page', () => {
  let mockApi: Awaited<ReturnType<typeof setupMockApi>> | null = null

  test.beforeEach(async ({ page }) => {
    if (!isRealMode) {
      mockApi = await setupMockApi(page)
    } else {
      mockApi = null
    }
    await loadWorkersPage(page, () => page.goto('/workers'))
  })

  test('displays workers page', async ({ page }) => {
    await expect(page.getByRole('heading', { name: 'Workers', level: 1 })).toBeVisible()
  })

  test('shows worker entries from API', async ({ page }) => {
    if (isRealMode) {
      // Real Docker mode: assert we render at least one worker card from the backend.
      const workerHeadings = page.getByRole('heading', { level: 3 })
      await expect(workerHeadings.first()).toBeVisible()
      await expect(page.getByText(/Online|Offline/).first()).toBeVisible()
      await expect(page.getByText('No workers registered')).toHaveCount(0)
      return
    }

    // Mock mode: default mock data includes worker-1 and worker-2
    await expect(page.getByRole('heading', { name: 'worker-1', level: 3 })).toBeVisible()
    await expect(page.getByRole('heading', { name: 'worker-2', level: 3 })).toBeVisible()

    // Status badges should render
    await expect(page.getByText('Online').first()).toBeVisible()
    await expect(page.getByText('Offline').first()).toBeVisible()
  })

  test('shows empty state when no workers', async ({ page }) => {
    if (isRealMode) {
      test.skip()
      return
    }

    mockApi?.clearWorkers()
    await loadWorkersPage(page, () => page.reload())

    await expect(page.getByText('No workers registered')).toBeVisible()
  })
})
