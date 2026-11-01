import { expect, type Page, test } from '@playwright/test'

import { type MockApiContext, setupMockApi } from './fixtures/mock-api'
import type { MockWorker } from './fixtures/mock-data'

/**
 * Tests for the Workers list: a table with host, status, registered tasks, started and last
 * seen. Mocked API responses, no Docker required; run with E2E_MODE=real for the Docker stack.
 */

const isRealMode = process.env.E2E_MODE === 'real'

const minutesAgo = (minutes: number): string =>
  new Date(Date.now() - minutes * 60_000).toISOString()

function worker(overrides: Partial<MockWorker> & Pick<MockWorker, 'hostname'>): MockWorker {
  return {
    pid: 100,
    registered_tasks: ['tasks.add'],
    status: 'online',
    registered_at: minutesAgo(120),
    last_seen: minutesAgo(1),
    ...overrides,
  }
}

/**
 * Navigate (or reload) and wait until the page has fetched its workers list.
 *
 * The app keeps retrying its WebSocket (no backend in mock mode), so `networkidle` isn't a
 * reliable "page is ready" signal; the page's own response is. The sidebar badge asks for the
 * same URL with `refresh=false`, so wait for the one without.
 */
async function loadWorkersPage(page: Page, navigate: () => Promise<unknown>): Promise<void> {
  const workersResponse = page.waitForResponse((response) => {
    const url = new URL(response.url())
    return /\/api\/workers$/.test(url.pathname) && !url.searchParams.has('refresh')
  })
  await navigate()
  await workersResponse
}

const table = (page: Page) => page.getByRole('table', { name: 'Workers' })
const rowOf = (page: Page, hostname: string) =>
  table(page)
    .getByRole('row')
    .filter({ has: page.getByRole('link', { name: hostname }) })

test.describe('Workers page', () => {
  let mockApi: MockApiContext | null = null

  test.beforeEach(async ({ page }) => {
    mockApi = isRealMode ? null : await setupMockApi(page)
  })

  test('shows the heading and how many workers are online', async ({ page }) => {
    await loadWorkersPage(page, () => page.goto('/workers'))
    await expect(page.getByRole('heading', { name: 'Workers', level: 1 })).toBeVisible()
    if (isRealMode) {
      await expect(page.getByText(/^\d+ of \d+ online$/)).toBeVisible()
      return
    }
    // Defaults: worker-1 online, worker-2 offline
    await expect(page.getByText('1 of 2 online')).toBeVisible()
  })

  test('lists one row per worker with host, status, registered tasks, started and last seen', async ({
    page,
  }) => {
    // The defaults' times are fixed when the fixtures load, so a long run ages them: set our own
    mockApi?.setWorkers([
      worker({
        hostname: 'worker-1',
        registered_tasks: Array.from({ length: 11 }, (_, i) => `tasks.t${i}`),
        registered_at: minutesAgo(60),
        last_seen: minutesAgo(2),
      }),
      worker({
        hostname: 'worker-2',
        status: 'offline',
        pid: 101,
        registered_at: minutesAgo(120),
        last_seen: minutesAgo(65),
      }),
    ])
    await loadWorkersPage(page, () => page.goto('/workers'))
    if (isRealMode) {
      // Real Docker mode: at least one worker row from the backend, never the empty state
      await expect(table(page).getByRole('row')).not.toHaveCount(1)
      await expect(page.getByTestId('worker-status').first()).toBeVisible()
      await expect(page.getByText('No workers registered')).toHaveCount(0)
      return
    }

    await expect(
      table(page)
        .getByRole('columnheader')
        .evaluateAll((cells) => cells.map((cell) => cell.textContent)),
    ).resolves.toEqual(['Hostname', 'Status', 'Registered tasks', 'Started', 'Last seen'])

    const first = rowOf(page, 'worker-1').getByRole('cell')
    await expect(first.nth(0)).toHaveText('worker-1')
    await expect(first.nth(1)).toHaveText('Online')
    await expect(first.nth(2)).toHaveText('11')
    await expect(first.nth(3)).toHaveText('1h ago')
    await expect(first.nth(4)).toHaveText('2m ago')

    const second = rowOf(page, 'worker-2').getByRole('cell')
    await expect(second.nth(1)).toHaveText('Offline')
    await expect(second.nth(2)).toHaveText('1')
    await expect(second.nth(4)).toHaveText('1h ago')
  })

  test('an offline worker is loud and an online one quiet', async ({ page }) => {
    test.skip(isRealMode, 'Mock-only test')
    await loadWorkersPage(page, () => page.goto('/workers'))
    const online = rowOf(page, 'worker-1').getByTestId('worker-status')
    const offline = rowOf(page, 'worker-2').getByTestId('worker-status')
    await expect(online).toHaveAttribute('data-variant', 'quiet')
    await expect(offline).toHaveAttribute('data-variant', 'loud')
    // Status is never colour alone: an icon and the word
    await expect(online.locator('svg')).toHaveCount(1)
    await expect(offline.locator('svg')).toHaveCount(1)
    // Only the offline row is tinted as a problem
    await expect(rowOf(page, 'worker-2')).toHaveAttribute('data-problem', 'true')
    await expect(rowOf(page, 'worker-1')).not.toHaveAttribute('data-problem', 'true')
  })

  test('a row opens the worker page, from any cell, and a nodename round-trips', async ({
    page,
  }) => {
    test.skip(isRealMode, 'Mock-only test')
    mockApi?.setWorkers([worker({ hostname: 'celery@alpha', registered_tasks: [] })])
    await loadWorkersPage(page, () => page.goto('/workers'))
    await expect(page.getByRole('link', { name: 'celery@alpha' })).toHaveAttribute(
      'href',
      /\/workers\/celery(@|%40)alpha$/,
    )
    // The registered count cell is part of the link's hit area
    // The link's overlay covers the cell, so Playwright sees it intercept: click the pixel
    await rowOf(page, 'celery@alpha').getByRole('cell').nth(2).click({ force: true })
    await expect(page).toHaveURL(/\/workers\/celery(@|%40)alpha$/)
    await expect(page.getByRole('heading', { name: 'celery@alpha', level: 1 })).toBeVisible()
  })

  test('shows no column for what the API does not send', async ({ page }) => {
    test.skip(isRealMode, 'Mock-only test')
    await loadWorkersPage(page, () => page.goto('/workers'))
    await expect(table(page).getByRole('columnheader', { name: 'Status' })).toBeVisible()
    for (const absent of ['Active', 'Pool', 'Queues', 'Concurrency']) {
      await expect(table(page).getByRole('columnheader', { name: absent })).toHaveCount(0)
    }
    // No placeholder dash stands in for them either
    await expect(table(page).getByText('—')).toHaveCount(0)
  })

  test('a worker with no registered tasks shows zero', async ({ page }) => {
    test.skip(isRealMode, 'Mock-only test')
    mockApi?.setWorkers([worker({ hostname: 'bare', registered_tasks: [] })])
    await loadWorkersPage(page, () => page.goto('/workers'))
    await expect(rowOf(page, 'bare').getByRole('cell').nth(2)).toHaveText('0')
  })

  test('the sidebar badge counts the offline workers and matches the rows', async ({ page }) => {
    test.skip(isRealMode, 'Mock-only test')
    mockApi?.setWorkers([
      worker({ hostname: 'a', status: 'offline' }),
      worker({ hostname: 'b', status: 'offline', pid: 101 }),
      worker({ hostname: 'c' }),
    ])
    await loadWorkersPage(page, () => page.goto('/workers'))
    const nav = page.getByRole('navigation', { name: 'Sections' })
    await expect(nav.getByRole('link', { name: /Workers/ })).toContainText('2 offline')
    await expect(page.locator('[data-testid="worker-status"][data-status="offline"]')).toHaveCount(
      2,
    )
  })

  test('the sidebar shows no badge when every worker is online', async ({ page }) => {
    test.skip(isRealMode, 'Mock-only test')
    mockApi?.setWorkers([worker({ hostname: 'a' })])
    await loadWorkersPage(page, () => page.goto('/workers'))
    const link = page
      .getByRole('navigation', { name: 'Sections' })
      .getByRole('link', { name: /Workers/ })
    await expect(link).toBeVisible()
    await expect(link).not.toContainText('offline')
  })

  test('the page asks for a refreshed list and the badge for the stored one', async ({ page }) => {
    test.skip(isRealMode, 'Mock-only test')
    const requests: string[] = []
    page.on('request', (request) => {
      const url = new URL(request.url())
      if (/\/api\/workers/.test(url.pathname)) requests.push(url.pathname + url.search)
    })
    await loadWorkersPage(page, () => page.goto('/workers'))
    // The badge's request lands after the page's, wait for it so the log is complete
    await expect.poll(() => requests.includes('/api/workers?refresh=false')).toBe(true)
    expect(requests.filter((r) => r === '/api/workers')).toHaveLength(1)
    expect(requests.filter((r) => r === '/api/workers?refresh=false')).toHaveLength(1)
  })

  // Status is decided per host: a restart leaves the old pid offline for a while, and that
  // must not make a host with a live process look down (list row, count, badge, host page).
  // The backend keeps the old pid's own last_seen, so an offline process can also be the NEWEST
  // sighting (event replay, clock skew): online must win whichever order they were seen in.
  for (const processes of [1, 2, 3]) {
    for (let live = 0; live <= processes; live++) {
      for (const offlineSeen of ['older', 'newer'] as const) {
        test(`host with ${processes} process(es), ${live} online, offline seen ${offlineSeen}: row, count, badge and page agree`, async ({
          page,
        }) => {
          test.skip(isRealMode, 'Mock-only test')
          const host = 'celery@h'
          const isOnline = (i: number) => i >= processes - live
          mockApi?.setWorkers([
            ...Array.from({ length: processes }, (_, i) =>
              worker({
                hostname: host,
                pid: 10 + i,
                status: isOnline(i) ? 'online' : 'offline',
                // Online: 2m, 3m... ago (the highest pid is the latest). Offline: an hour ago, or
                // seconds ago, newer than every online one.
                last_seen: minutesAgo(
                  isOnline(i)
                    ? 1 + (processes - i)
                    : offlineSeen === 'older'
                      ? 60 + i
                      : 0.1 + i / 100,
                ),
              }),
            ),
            worker({ hostname: 'celery@other', pid: 99 }),
          ])
          await loadWorkersPage(page, () => page.goto('/workers'))
          const up = live > 0
          // The leading process: the newest online one, else the newest offline one (pid 10)
          const leadingPid = up ? 10 + processes - 1 : 10
          const status = up ? 'online' : 'offline'
          await expect(table(page).getByRole('row')).toHaveCount(3) // header, host, other
          await expect(rowOf(page, host)).toHaveCount(1)
          await expect(rowOf(page, host).getByTestId('worker-status')).toHaveAttribute(
            'data-status',
            status,
          )
          await expect(page.getByText(`${up ? 2 : 1} of 2 online`)).toBeVisible()
          const badge = page
            .getByRole('navigation', { name: 'Sections' })
            .getByRole('link', { name: /Workers/ })
          if (up) await expect(badge).not.toContainText('offline')
          else await expect(badge).toContainText('1 offline')
          await expect(badge).not.toContainText(/[2-9] offline/)

          await rowOf(page, host).getByRole('link').click()
          await expect(page.getByTestId('worker-status')).toHaveAttribute('data-status', status)
          await expect(page.getByRole('note')).toHaveCount(up ? 0 : 1)
          await expect(
            page.getByRole('region', { name: 'Details' }).locator('dd').first(),
          ).toHaveText(String(leadingPid))
        })
      }
    }
  }

  test('shows an empty state when no workers', async ({ page }) => {
    test.skip(isRealMode, 'Mock-only test')
    mockApi?.clearWorkers()
    await loadWorkersPage(page, () => page.goto('/workers'))
    await expect(page.getByText('No workers registered')).toBeVisible()
    await expect(page.getByText('0 of 0 online')).toHaveCount(0)
    await expect(table(page)).toHaveCount(0)
  })

  test('says so when the workers cannot be loaded', async ({ page }) => {
    test.skip(isRealMode, 'Mock-only test')
    await page.route(/\/api\/workers$/, (route) => route.fulfill({ status: 500, body: 'boom' }))
    await page.goto('/workers')
    await expect(page.getByText('Failed to load workers')).toBeVisible({ timeout: 15_000 })
  })

  for (const theme of ['light', 'dark'] as const) {
    test(`the offline row and chip use the failure tokens in ${theme}`, async ({ page }) => {
      test.skip(isRealMode, 'Mock-only test')
      await page.addInitScript((t) => localStorage.setItem('stemtrace-theme', t), theme)
      await loadWorkersPage(page, () => page.goto('/workers'))
      const block = rowOf(page, 'worker-2')
        .getByTestId('worker-status')
        .locator('[data-status-icon]')
      const got = await block.evaluate((el) => {
        const probe = document.createElement('div')
        probe.style.backgroundColor = 'var(--failure)'
        document.body.append(probe)
        const want = getComputedStyle(probe).backgroundColor
        probe.remove()
        return { actual: getComputedStyle(el).backgroundColor, want }
      })
      expect(got.actual).toBe(got.want)
    })
  }

  test('at 400px the page does not scroll sideways', async ({ page }) => {
    test.skip(isRealMode, 'Mock-only test')
    await page.setViewportSize({ width: 400, height: 800 })
    await loadWorkersPage(page, () => page.goto('/workers'))
    await expect(rowOf(page, 'worker-1')).toBeVisible()
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(400)
  })
})
