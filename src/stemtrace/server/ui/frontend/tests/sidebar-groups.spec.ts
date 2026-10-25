import { expect, type Locator, type Page, test } from '@playwright/test'

import { setupMockApi } from './fixtures/mock-api'
import { createSuccessTask, type MockGroup } from './fixtures/mock-data'

/**
 * The sidebar's Groups and Tags sections, from GET /api/groups: hidden when the endpoint is
 * missing or the list is empty; each entry opens Executions filtered by it. Mock mode only.
 */

test.skip(process.env.E2E_MODE === 'real', 'Mock-only test')

const sidebar = (page: Page): Locator =>
  page.getByRole('complementary', { name: 'Primary navigation' })
const groupsNav = (page: Page): Locator => sidebar(page).getByRole('navigation', { name: 'Groups' })
const tagsNav = (page: Page): Locator => sidebar(page).getByRole('navigation', { name: 'Tags' })

const GROUPS: MockGroup[] = [
  { name: 'Billing', source: 'code', tags: ['money', 'critical'], color: null, task_names: [] },
  { name: 'Search', source: 'auto', tags: [], color: null, task_names: [] },
  { name: 'Email', source: 'code', tags: ['critical'], color: null, task_names: [] },
]

test.describe('Sidebar groups and tags', () => {
  test('are absent when /api/groups answers 404', async ({ page }) => {
    const api = await setupMockApi(page, { useDefaults: false })
    api.setGroups(null)
    await page.goto('/executions')
    await expect(sidebar(page).getByRole('link', { name: 'Executions' })).toBeVisible()
    await expect(sidebar(page).getByText('Groups', { exact: true })).toHaveCount(0)
    await expect(sidebar(page).getByText('Tags', { exact: true })).toHaveCount(0)
  })

  test('are absent when there are no groups', async ({ page }) => {
    const api = await setupMockApi(page, { useDefaults: false })
    api.setGroups([])
    await page.goto('/executions')
    await expect(sidebar(page).getByRole('link', { name: 'Executions' })).toBeVisible()
    await expect(groupsNav(page)).toHaveCount(0)
    await expect(tagsNav(page)).toHaveCount(0)
  })

  test('list each group and each distinct tag once, with no failure percentage', async ({
    page,
  }) => {
    const api = await setupMockApi(page, { useDefaults: false })
    api.setGroups(GROUPS)
    await page.goto('/executions')
    await expect(groupsNav(page).getByRole('link')).toHaveText(['Billing', 'Search', 'Email'])
    await expect(tagsNav(page).getByRole('link')).toHaveText(['#critical', '#money'])
    await expect(groupsNav(page)).not.toContainText('%')
  })

  test('there is no Tags section when no group has a tag', async ({ page }) => {
    const api = await setupMockApi(page, { useDefaults: false })
    api.setGroups([GROUPS[1]])
    await page.goto('/executions')
    await expect(groupsNav(page).getByRole('link')).toHaveText(['Search'])
    await expect(tagsNav(page)).toHaveCount(0)
  })

  test('a group link opens Executions filtered by the group', async ({ page }) => {
    const api = await setupMockApi(page, { useDefaults: false })
    api.setGroups(GROUPS)
    api.addTask(createSuccessTask('app.jobs.one'))
    const queries: Record<string, string>[] = []
    page.on('request', (r) => {
      const url = new URL(r.url())
      if (url.pathname === '/api/tasks') queries.push(Object.fromEntries(url.searchParams))
    })
    await page.goto('/workers')
    await groupsNav(page).getByRole('link', { name: 'Billing' }).click()
    await expect(page).toHaveURL(/\/executions\?task_group=Billing$/)
    await expect(page.locator('[data-chip="task_group"]')).toContainText('Billing')
    await expect
      .poll(() => queries.at(-1))
      .toEqual({ limit: '200', offset: '0', compact: '1', task_group: 'Billing' })
    await expect(groupsNav(page).getByRole('link', { name: 'Billing' })).toHaveAttribute(
      'aria-current',
      'page',
    )
    await expect(groupsNav(page).getByRole('link', { name: 'Search' })).not.toHaveAttribute(
      'aria-current',
      'page',
    )
  })

  test('a tag link opens Executions filtered by the tag', async ({ page }) => {
    const api = await setupMockApi(page, { useDefaults: false })
    api.setGroups(GROUPS)
    api.addTask(createSuccessTask('app.jobs.one'))
    const queries: Record<string, string>[] = []
    page.on('request', (r) => {
      const url = new URL(r.url())
      if (url.pathname === '/api/tasks') queries.push(Object.fromEntries(url.searchParams))
    })
    await page.goto('/executions?queue=default')
    await tagsNav(page).getByRole('link', { name: '#critical' }).click()
    // A sidebar entry replaces the filters with its own: it is a jump, not an addition
    await expect(page).toHaveURL(/\/executions\?tag=critical$/)
    await expect
      .poll(() => queries.at(-1))
      .toEqual({ limit: '200', offset: '0', compact: '1', tag: 'critical' })
    await expect(tagsNav(page).getByRole('link', { name: '#critical' })).toHaveAttribute(
      'aria-current',
      'page',
    )
  })

  test('are in the drawer at 400px and a link closes it', async ({ page }) => {
    await page.setViewportSize({ width: 400, height: 800 })
    const api = await setupMockApi(page, { useDefaults: false })
    api.setGroups(GROUPS)
    await page.goto('/workers')
    await page.getByRole('button', { name: 'Open navigation' }).click()
    await expect(sidebar(page)).toBeInViewport({ ratio: 1 })
    await expect(groupsNav(page).getByRole('link', { name: 'Billing' })).toBeVisible()
    await groupsNav(page).getByRole('link', { name: 'Billing' }).click()
    await expect(page).toHaveURL(/task_group=Billing/)
    await expect(sidebar(page)).not.toBeInViewport()
  })
})
