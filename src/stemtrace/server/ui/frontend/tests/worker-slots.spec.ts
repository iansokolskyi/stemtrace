import { expect, type Locator, type Page, test } from '@playwright/test'

import { type MockApiContext, setupMockApi } from './fixtures/mock-api'
import type { MockWorker } from './fixtures/mock-data'
import { mockOverview, spikeScenario } from './fixtures/overview'

/**
 * Pool and slot meters on the Overview's worker cells and in the Workers panel (U5). A field the
 * server doesn't send hides its part of the UI; no cell or row shows "–" for a missing value.
 * Mock mode only: each test shapes the `/api/workers` payload.
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

async function openOverview(
  page: Page,
  workers: MockWorker[],
  path = '/',
): Promise<MockApiContext> {
  const api = await setupMockApi(page)
  api.setWorkers(workers)
  await mockOverview(page, spikeScenario())
  await page.goto(path)
  await expect(page.getByTestId('worker-strip')).toBeVisible()
  return api
}

const cell = (page: Page, host: string): Locator =>
  page.getByTestId('worker-strip').getByRole('button', { name: new RegExp(`^${host}\\b`) })
const meterOf = (host: Locator): Locator => host.getByTestId('slot-meter')
const fillOf = (host: Locator): Locator => meterOf(host).locator('i')
const poolLine = (host: Locator): Locator => host.getByTestId('pool-line')
const panelOf = (page: Page): Locator => page.getByTestId('triage-panel')

test.describe('Worker cell pool line and meter', () => {
  test.skip(isRealMode, 'Mock-only test')

  test('a prefork pool with active slots reads "3 of 8 slots active" and a 37.5% bar', async ({
    page,
  }) => {
    await openOverview(page, [
      worker({ hostname: 'web-1', pool: 'prefork', concurrency: 8, active: 3 }),
    ])
    const web = cell(page, 'web-1')
    await expect(poolLine(web)).toHaveText('prefork pool, 3 of 8 slots active')
    await expect(fillOf(web)).toHaveAttribute('style', /width:\s*37\.5%/)
    await expect(meterOf(web)).toHaveCSS('height', '6px')
  })

  test('the threads pool and an idle worker read "0 of 4 slots active" with an empty bar', async ({
    page,
  }) => {
    await openOverview(page, [
      worker({ hostname: 'web-1', pool: 'threads', concurrency: 4, active: 0 }),
    ])
    const web = cell(page, 'web-1')
    await expect(poolLine(web)).toHaveText('threads pool, 0 of 4 slots active')
    await expect(fillOf(web)).toHaveAttribute('style', /width:\s*0%/)
  })

  test('active above concurrency shows the real numbers and a full bar', async ({ page }) => {
    await openOverview(page, [
      worker({ hostname: 'web-1', pool: 'prefork', concurrency: 8, active: 9 }),
    ])
    const web = cell(page, 'web-1')
    await expect(poolLine(web)).toHaveText('prefork pool, 9 active, 8 slots')
    await expect(fillOf(web)).toHaveAttribute('style', /width:\s*100%/)
  })

  for (const active of [null, undefined]) {
    test(`without active (${active}) the line reads "prefork pool, 8 slots" and has no bar`, async ({
      page,
    }) => {
      await openOverview(page, [
        worker({ hostname: 'web-1', pool: 'prefork', concurrency: 8, active }),
      ])
      const web = cell(page, 'web-1')
      await expect(poolLine(web)).toHaveText('prefork pool, 8 slots')
      await expect(meterOf(web)).toHaveCount(0)
    })
  }

  test('with active but no concurrency the line says "3 active" and has no bar', async ({
    page,
  }) => {
    await openOverview(page, [
      worker({ hostname: 'web-1', pool: 'prefork', concurrency: null, active: 3 }),
    ])
    const web = cell(page, 'web-1')
    await expect(poolLine(web)).toHaveText('prefork pool, 3 active')
    await expect(meterOf(web)).toHaveCount(0)
  })

  test('a pool the server cannot name still shows its slots', async ({ page }) => {
    await openOverview(page, [worker({ hostname: 'web-1', pool: null, concurrency: 8, active: 3 })])
    const web = cell(page, 'web-1')
    await expect(poolLine(web)).toHaveText('3 of 8 slots active')
    await expect(fillOf(web)).toHaveAttribute('style', /width:\s*37\.5%/)
  })

  test('an offline host has no active, so its line has no bar', async ({ page }) => {
    await openOverview(page, [
      worker({
        hostname: 'web-1',
        status: 'offline',
        last_seen: minutesAgo(90),
        pool: 'prefork',
        concurrency: 8,
        active: null,
      }),
    ])
    const web = cell(page, 'web-1')
    await expect(web).toContainText('Offline')
    await expect(poolLine(web)).toHaveText('prefork pool, 8 slots')
    await expect(meterOf(web)).toHaveCount(0)
  })

  test('with no pool info the line and the bar are hidden, and nothing reads "–"', async ({
    page,
  }) => {
    await openOverview(page, [
      worker({ hostname: 'web-1', pool: null, concurrency: null, active: null }),
    ])
    const web = cell(page, 'web-1')
    await expect(web).toContainText('1 registered tasks')
    await expect(poolLine(web)).toHaveCount(0)
    await expect(meterOf(web)).toHaveCount(0)
    await expect(web).not.toContainText('–')
  })

  test('an older server without any of the fields shows the cell as before', async ({ page }) => {
    await openOverview(page, [worker({ hostname: 'web-1' })])
    const web = cell(page, 'web-1')
    await expect(web).toContainText('1 registered tasks')
    await expect(poolLine(web)).toHaveCount(0)
    await expect(meterOf(web)).toHaveCount(0)
    await expect(web).not.toContainText(/–|undefined|NaN|null/)
  })

  test('active without any pool info stays hidden', async ({ page }) => {
    await openOverview(page, [
      worker({ hostname: 'web-1', pool: null, concurrency: null, active: 3 }),
    ])
    const web = cell(page, 'web-1')
    await expect(poolLine(web)).toHaveCount(0)
    await expect(meterOf(web)).toHaveCount(0)
  })

  test('a host with a stopped old pid and a live new one shows the live pid', async ({ page }) => {
    await openOverview(page, [
      worker({
        hostname: 'web-1',
        pid: 1,
        status: 'offline',
        last_seen: minutesAgo(30),
        pool: 'prefork',
        concurrency: 2,
        active: null,
      }),
      worker({ hostname: 'web-1', pid: 2, pool: 'prefork', concurrency: 8, active: 5 }),
    ])
    await expect(poolLine(cell(page, 'web-1'))).toHaveText('prefork pool, 5 of 8 slots active')
  })

  test('a poll that brings a new count moves the text and the bar', async ({ page }) => {
    await page.clock.install()
    const api = await openOverview(page, [
      worker({ hostname: 'web-1', pool: 'prefork', concurrency: 8, active: 2 }),
    ])
    await expect(poolLine(cell(page, 'web-1'))).toHaveText('prefork pool, 2 of 8 slots active')
    api.setWorkers([worker({ hostname: 'web-1', pool: 'prefork', concurrency: 8, active: 4 })])
    await page.clock.runFor(16_000)
    await expect(poolLine(cell(page, 'web-1'))).toHaveText('prefork pool, 4 of 8 slots active')
    await expect(fillOf(cell(page, 'web-1'))).toHaveAttribute('style', /width:\s*50%/)
  })

  test('the cell still opens the worker in the panel', async ({ page }) => {
    await openOverview(page, [
      worker({ hostname: 'web-1', pool: 'prefork', concurrency: 8, active: 3 }),
    ])
    await cell(page, 'web-1').click()
    await expect(panelOf(page).getByRole('heading', { level: 2 })).toHaveText('web-1')
  })
})

test.describe('Workers panel rows', () => {
  test.skip(isRealMode, 'Mock-only test')

  const row = (page: Page, host: string): Locator =>
    panelOf(page).getByRole('listitem').filter({ hasText: host })

  test('a live row reads "N of M slots", and rows without the numbers show nothing', async ({
    page,
  }) => {
    await openOverview(
      page,
      [
        worker({ hostname: 'celery@alpha', pool: 'prefork', concurrency: 8, active: 3 }),
        worker({ hostname: 'celery@bravo', pool: 'prefork', concurrency: 8, active: null }),
        worker({ hostname: 'celery@charlie', pool: 'prefork', concurrency: null, active: 2 }),
        worker({ hostname: 'celery@delta' }),
        worker({
          hostname: 'celery@echo',
          status: 'offline',
          last_seen: minutesAgo(90),
          pool: 'prefork',
          concurrency: 8,
          active: null,
        }),
      ],
      '/?panel=worker%3Aall',
    )
    await expect(row(page, 'alpha').getByTestId('slot-count')).toHaveText('3 of 8 slots')
    for (const host of ['bravo', 'charlie', 'delta', 'echo']) {
      await expect(row(page, host).getByTestId('slot-count')).toHaveCount(0)
      await expect(row(page, host)).not.toContainText(/slots|–/)
    }
  })

  test('active above concurrency shows the real numbers', async ({ page }) => {
    await openOverview(
      page,
      [worker({ hostname: 'celery@alpha', pool: 'prefork', concurrency: 8, active: 9 })],
      '/?panel=worker%3Aall',
    )
    await expect(row(page, 'alpha').getByTestId('slot-count')).toHaveText('9 of 8 slots')
  })
})
