import { expect, type Locator, type Page, test } from '@playwright/test'

import { setupMockApi } from './fixtures/mock-api'
import type { MockGroup } from './fixtures/mock-data'
import { tokenColour } from './fixtures/task-page'

/**
 * The sidebar details of the prototype: the broker under the brand, each group's failure rate
 * over the range, tags as chips, "Live streaming events" and the theme button on its own line.
 * Mock mode only.
 */

test.skip(process.env.E2E_MODE === 'real', 'Mock-only test')

const sidebar = (page: Page): Locator =>
  page.getByRole('complementary', { name: 'Primary navigation' })
const groupLink = (page: Page, name: string | RegExp): Locator =>
  sidebar(page).getByRole('navigation', { name: 'Groups' }).getByRole('link', { name })
const rate = (page: Page, name: string | RegExp): Locator =>
  groupLink(page, name).locator('[title^="Failure rate"]')

const group = (name: string, tags: string[] = []): MockGroup => ({
  name,
  source: 'code',
  tags,
  color: null,
  task_names: [],
})

/** Runs per bucket for each state, in two buckets. */
const counts = (success: number, failure: number): Record<string, number[]> => ({
  SUCCESS: [success, 0],
  FAILURE: [0, failure],
})

test.describe('Sidebar broker label', () => {
  test('shows the broker kind under the brand', async ({ page }) => {
    const api = await setupMockApi(page, { useDefaults: false })
    api.setBroker('redis')
    await page.goto('/executions')
    await expect(sidebar(page).getByText('Broker: redis', { exact: true })).toBeVisible()
    await expect(sidebar(page).getByText('stemtrace', { exact: true })).toBeVisible()
    await expect(sidebar(page).getByText('Broker: redis')).toHaveCSS(
      'color',
      await tokenColour(page, 'muted'),
    )
    await expect(sidebar(page).getByText('Environment')).toHaveCount(0)
  })

  test('is left out when the server reports no broker', async ({ page }) => {
    const api = await setupMockApi(page, { useDefaults: false })
    api.setBroker(null)
    await page.goto('/executions')
    await expect(sidebar(page).getByRole('link', { name: 'Executions' })).toBeVisible()
    await expect(sidebar(page).getByText(/Broker/)).toHaveCount(0)
  })
})

test.describe('Sidebar group failure rate', () => {
  const GROUPS = [group('Billing'), group('Search'), group('Email'), group('Idle')]
  const STATS = {
    Billing: counts(90, 10),
    Search: counts(50, 0),
    Email: counts(197, 3),
    // Absent from the answer: no runs in the range
  }

  test('shows failures over runs, in the failure colour above 1%, muted otherwise', async ({
    page,
  }) => {
    const api = await setupMockApi(page, { useDefaults: false })
    api.setGroups(GROUPS)
    api.setGroupStats(STATS)
    await page.goto('/executions')
    await expect(rate(page, /Billing/)).toHaveText('10%')
    await expect(rate(page, /Search/)).toHaveText('0%')
    await expect(rate(page, /Email/)).toHaveText('1.5%')
    await expect(rate(page, /Idle/)).toHaveText('0%')

    const failure = await tokenColour(page, 'failure-t')
    const muted = await tokenColour(page, 'muted')
    await expect(rate(page, /Billing/)).toHaveCSS('color', failure)
    await expect(rate(page, /Email/)).toHaveCSS('color', failure)
    await expect(rate(page, /Search/)).toHaveCSS('color', muted)
    await expect(rate(page, /Idle/)).toHaveCSS('color', muted)
    await expect(rate(page, /Billing/)).toHaveAttribute('title', 'Failure rate, the last 6h')
  })

  test('exactly 1% is not a failure colour', async ({ page }) => {
    const api = await setupMockApi(page, { useDefaults: false })
    api.setGroups([group('Billing')])
    api.setGroupStats({ Billing: counts(99, 1) })
    await page.goto('/executions')
    await expect(rate(page, /Billing/)).toHaveText('1.0%')
    await expect(rate(page, /Billing/)).toHaveCSS('color', await tokenColour(page, 'muted'))
  })

  test('asks once for all groups, for the range the Executions page shows', async ({ page }) => {
    const api = await setupMockApi(page, { useDefaults: false })
    api.setGroups(GROUPS)
    api.setGroupStats(STATS)
    await page.goto('/executions')
    await expect(rate(page, /Billing/)).toBeVisible()
    expect(api.statsRequests.map((url) => [...url.searchParams])).toEqual([
      [
        ['within', '6h'],
        ['group_by', 'task_group'],
      ],
    ])
  })

  test('follows the range: 1h asks for 1h, All asks for the whole retention', async ({ page }) => {
    const api = await setupMockApi(page, { useDefaults: false })
    api.setGroups(GROUPS)
    api.setGroupStats(STATS)
    await page.goto('/executions?range=1h')
    await expect(rate(page, /Billing/)).toBeVisible()
    expect(api.statsRequests.map((url) => [...url.searchParams]).at(-1)).toEqual([
      ['within', '1h'],
      ['group_by', 'task_group'],
    ])
    await expect(rate(page, /Billing/)).toHaveAttribute('title', 'Failure rate, the last hour')

    await page.getByRole('button', { name: 'All', exact: true }).click()
    await expect(rate(page, /Billing/)).toHaveAttribute('title', 'Failure rate')
    await expect
      .poll(() => api.statsRequests.map((url) => [...url.searchParams]).at(-1))
      .toEqual([['group_by', 'task_group']])
  })

  test('shows no rate when the endpoint is missing, and does not ask again', async ({ page }) => {
    const api = await setupMockApi(page, { useDefaults: false })
    api.setGroups(GROUPS)
    api.setGroupStats(null)
    await page.goto('/executions')
    await expect(groupLink(page, /Billing/)).toBeVisible()
    await expect(sidebar(page).locator('[title^="Failure rate"]')).toHaveCount(0)
    await expect(groupLink(page, /Billing/)).toHaveText('Billing')
    expect(api.statsRequests).toHaveLength(1)
  })

  test('shows no rate when the endpoint fails', async ({ page }) => {
    const api = await setupMockApi(page, { useDefaults: false })
    api.setGroups(GROUPS)
    api.setGroupStats('error')
    await page.goto('/executions')
    await expect(groupLink(page, /Billing/)).toBeVisible()
    await expect(sidebar(page).locator('[title^="Failure rate"]')).toHaveCount(0)
  })

  test('a long group name is cut, the rate stays whole at 400px, with no page scroll', async ({
    page,
  }) => {
    await page.setViewportSize({ width: 400, height: 800 })
    const long = 'a-very-long-group-name-that-cannot-fit-the-drawer-width-at-all'
    const api = await setupMockApi(page, { useDefaults: false })
    api.setGroups([group(long)])
    api.setGroupStats({ [long]: counts(1, 9) })
    await page.goto('/workers')
    await page.getByRole('button', { name: 'Open navigation' }).click()
    await expect(rate(page, long)).toHaveText('90%')
    await expect(rate(page, long)).toBeInViewport({ ratio: 1 })
    expect(
      await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth),
    ).toBe(true)
  })
})

test.describe('Sidebar tags', () => {
  test('are wrapping chips, several to a row', async ({ page }) => {
    const api = await setupMockApi(page, { useDefaults: false })
    api.setGroups([group('Billing', ['money', 'critical', 'nightly', 'slow', 'beta', 'external'])])
    await page.goto('/executions')
    const chips = sidebar(page).getByRole('navigation', { name: 'Tags' }).getByRole('link')
    await expect(chips).toHaveText([
      '#beta',
      '#critical',
      '#external',
      '#money',
      '#nightly',
      '#slow',
    ])
    const tops = await chips.evaluateAll((els) =>
      els.map((el) => Math.round(el.getBoundingClientRect().top)),
    )
    // Chips share rows (fewer rows than chips) and wrap (more than one row)
    const rows = new Set(tops)
    expect(rows.size).toBeGreaterThan(1)
    expect(rows.size).toBeLessThan(tops.length)
    await expect(chips.first()).toHaveCSS('background-color', await tokenColour(page, 'raised'))
  })
})

test.describe('Sidebar footer', () => {
  test('says "Live streaming events" while the socket is open', async ({ page }) => {
    await page.routeWebSocket(/\/ws$/, () => {})
    await setupMockApi(page)
    await page.goto('/executions')
    const status = page.getByTestId('live-status')
    await expect(status).toHaveText('Live streaming events')
    await expect(status.getByText('streaming events')).toHaveCSS(
      'color',
      await tokenColour(page, 'muted'),
    )
  })

  test('has no caption while polling', async ({ page }) => {
    // A socket that fails at once, as frame.spec.ts does
    await page.addInitScript(() => {
      class FailingWebSocket extends EventTarget {
        static readonly CONNECTING = 0
        static readonly OPEN = 1
        static readonly CLOSING = 2
        static readonly CLOSED = 3
        readyState = 3
        onopen: (() => void) | null = null
        onmessage: (() => void) | null = null
        onerror: (() => void) | null = null
        onclose: (() => void) | null = null
        constructor() {
          super()
          setTimeout(() => {
            this.onerror?.()
            this.onclose?.()
          }, 0)
        }
        close(): void {}
        send(): void {}
      }
      Object.defineProperty(window, 'WebSocket', { value: FailingWebSocket })
    })
    await setupMockApi(page)
    await page.goto('/executions')
    await expect(page.getByTestId('live-status')).toHaveText('Polling')
  })

  test('the theme button is on its own line, under the live status and above the version', async ({
    page,
  }) => {
    await page.routeWebSocket(/\/ws$/, () => {})
    await setupMockApi(page)
    await page.goto('/executions')
    const live = await page.getByTestId('live-status').boundingBox()
    const theme = await sidebar(page)
      .getByRole('button', { name: /Switch to/ })
      .boundingBox()
    const version = await sidebar(page)
      .getByText(/^Version /)
      .boundingBox()
    expect(live && theme && version).toBeTruthy()
    expect(theme?.y ?? 0).toBeGreaterThanOrEqual((live?.y ?? 0) + (live?.height ?? 0))
    expect(version?.y ?? 0).toBeGreaterThanOrEqual((theme?.y ?? 0) + (theme?.height ?? 0))
  })
})
