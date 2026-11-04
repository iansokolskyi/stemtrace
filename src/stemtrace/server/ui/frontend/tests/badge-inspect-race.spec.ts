import { expect, type Page, type Route, test } from '@playwright/test'

import { setupMockApi } from './fixtures/mock-api'
import { createAnomaly, type MockWorker } from './fixtures/mock-data'

/**
 * A cold load asks `/api/anomalies` for the sidebar badge (and, on /anomalies, for the list) and
 * `/api/workers` for the Workers page while the badge's inspect is still running. Each must read
 * the state the inspect found.
 *
 * The mock models the server's `refresh=true` handling. By default a request that arrives during
 * an inspect waits for it (the current server, `_InspectRefresher`). With `OLD_SERVER_MOCK=1` it
 * answers at once with the state from before the inspect (the old server, which lost the
 * single-flight race): this spec must fail then, which shows it can detect the bug.
 */

test.skip(process.env.E2E_MODE === 'real', 'Mock-only test')

const joinsInflight = process.env.OLD_SERVER_MOCK !== '1'
const INSPECT_MS = 600
const HOST = 'celery@worker-1'
const iso = (minutesAgo: number): string => new Date(Date.now() - minutesAgo * 60_000).toISOString()

const worker = (status: MockWorker['status']): MockWorker => ({
  hostname: HOST,
  pid: 100,
  registered_tasks: ['tasks.add'],
  status,
  registered_at: iso(120),
  last_seen: iso(1),
})

const unregistered = createAnomaly('unregistered', {
  name: 'billing.tasks.sync_invoices_v2',
  since: iso(55),
})

/** The inspect finds the worker online and the renamed task unregistered; before it, neither. */
async function coldLoad(page: Page, path: string): Promise<string[]> {
  await page.routeWebSocket(/\/ws$/, () => {})
  await setupMockApi(page, { useDefaults: false })
  let inspected = false
  let inflight: Promise<void> | null = null
  let markFirstRequest: () => void = () => {}
  const firstRequest = new Promise<void>((resolve) => {
    markFirstRequest = resolve
  })
  const log: string[] = []

  const refresh = async (): Promise<void> => {
    if (inflight) {
      if (joinsInflight) await inflight
      return
    }
    inflight = new Promise<void>((resolve) =>
      setTimeout(() => {
        inspected = true
        inflight = null
        resolve()
      }, INSPECT_MS),
    )
    await inflight
  }
  const asksWorkers = (route: Route): boolean =>
    new URL(route.request().url()).searchParams.get('refresh') !== 'false'

  await page.route(/\/api\/anomalies(\?|$)/, async (route: Route) => {
    markFirstRequest()
    if (asksWorkers(route)) await refresh()
    log.push(`anomalies:fresh=${inspected}`)
    const items = inspected ? [unregistered] : []
    const counts = { stuck: 0, interrupted: 0, unregistered: items.length, no_workers_online: 0 }
    await route
      .fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          anomalies: items,
          counts,
          total: items.length,
          limit: 100,
          offset: 0,
          evaluated_at: new Date().toISOString(),
        }),
      })
      .catch(() => {})
  })

  await page.route(/\/api\/workers(\?|$)/, async (route: Route) => {
    if (asksWorkers(route)) {
      // The badge's anomaly request is the one that starts the inspect
      await firstRequest
      await refresh()
    }
    log.push(`workers:fresh=${inspected}`)
    const workers = [worker(inspected ? 'online' : 'offline')]
    await route
      .fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ workers, total: workers.length }),
      })
      .catch(() => {})
  })

  await page.goto(path)
  return log
}

test.describe('Cold load while the badge inspect is in flight', () => {
  test('Workers shows the worker Online after the inspect', async ({ page }) => {
    const log = await coldLoad(page, '/workers')
    const row = page.getByRole('row', { name: new RegExp(HOST) })
    await expect(row).toBeVisible()
    await expect(row).toContainText('Online')
    expect(log).toContain('workers:fresh=true')
  })

  test('Anomalies lists the unregistered item after the inspect', async ({ page }) => {
    await coldLoad(page, '/anomalies')
    const listed = page.locator('[data-testid="anomaly-item"][data-kind="unregistered"]')
    await expect(listed).toHaveCount(1)
    await expect(listed).toContainText('billing.tasks.sync_invoices_v2')
    await expect(
      page.getByRole('navigation', { name: 'Sections' }).getByRole('link', { name: /Anomalies/ }),
    ).toHaveText('Anomalies1')
  })
})
