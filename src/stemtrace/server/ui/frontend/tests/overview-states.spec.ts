import { expect, type Page, test } from '@playwright/test'

import { setupMockApi } from './fixtures/mock-api'
import { mockOverview, spikeScenario } from './fixtures/overview'
import {
  ANOMALIES_ENDPOINT,
  delayEndpoints,
  emptySummaryEndpoints,
  FAILURES_ENDPOINT,
  failSummaryEndpoints,
  GROUP_SERIES_ENDPOINT,
  OVERVIEW_ENDPOINT,
  SERIES_ENDPOINT,
  STATS_ENDPOINT,
  WORKERS_ENDPOINT,
} from './fixtures/overview-states'

/**
 * The Overview's view states (ui-restyle-audit G3, readings O1-O17). Mock mode only. One rule
 * decides every part: no data and no attempt yet is loading; no data and the last attempt failed
 * is an error box that stays while it retries; data held with a failed last attempt keeps the
 * data and shows the page's stale line.
 */

const NOW = new Date('2026-10-17T12:00:00Z')
// A first load that fails is retried 3 times (1 s, 2 s, 4 s) before its box shows
const AFTER_RETRIES = { timeout: 20_000 }

async function open(
  page: Page,
  shape: (page: Page) => Promise<unknown>,
  { clock = false, loginPath = false }: { clock?: boolean; loginPath?: boolean } = {},
): Promise<void> {
  await page.routeWebSocket(/\/ws$/, () => {})
  if (clock) await page.clock.install({ time: NOW })
  if (loginPath) {
    await page.addInitScript(() => {
      // Form login is on; the redirect itself is held off by its cooldown so the page stays
      window.__STEMTRACE_LOGOUT_PATH__ = '/logout'
      window.sessionStorage.setItem('stemtrace:login-redirect-at', String(Date.now()))
    })
  }
  const api = await setupMockApi(page)
  api.clearTasks()
  await mockOverview(page, spikeScenario(NOW.getTime()))
  await shape(page)
  await page.goto('/')
  await expect(page.getByRole('heading', { level: 1, name: 'Overview' })).toBeVisible()
}

const subtitle = (page: Page) => page.getByRole('heading', { level: 1 }).locator('xpath=../p')
const staleLine = (page: Page) => page.getByRole('status').filter({ hasText: "Couldn't refresh." })

/**
 * Moves the fake clock in 1 s steps, so each retry timer a failed request schedules is created
 * (after its response arrives in real time) before the clock passes it: one big jump would leave
 * the retries unrun.
 */
async function advance(page: Page, ms: number): Promise<void> {
  for (let elapsed = 0; elapsed < ms; elapsed += 1_000) {
    await page.clock.runFor(1_000)
    await page.waitForTimeout(40)
  }
}

/** Past the next poll (15 s; 30 s for anomalies) and the retries after a failed one. */
async function pollAndRetry(page: Page): Promise<void> {
  await advance(page, 31_000)
  await advance(page, 10_000)
  // Stop between two polls: while one is retrying, the stale line's Retry reads "Retrying…"
  const idle = staleLine(page).getByRole('button', { name: 'Retry', exact: true })
  for (let step = 0; step < 20 && (await idle.count()) === 0; step++) await advance(page, 1_000)
}

test.describe('Overview states', () => {
  test.skip(process.env.E2E_MODE === 'real', 'mock-only: the endpoints are made to fail or empty')
  test.setTimeout(90_000)

  test('failed first loads say so in every part and offer Retry', async ({ page }) => {
    let state = { failing: true }
    await open(page, async (p) => {
      state = await failSummaryEndpoints(p)
    })

    await expect(page.getByRole('alert')).toHaveCount(5, AFTER_RETRIES)
    await expect(
      page.getByRole('alert').filter({ hasText: "Couldn't load the overview." }),
    ).toBeVisible()
    await expect(page.getByTestId('runs-chart')).toContainText("Couldn't load the runs.")
    await expect(page.getByTestId('runs-chart')).not.toContainText('No runs in this window.')
    await expect(page.getByTestId('runs-chart')).toContainText('The server answered 500.')
    // Only the tiles the overview feeds are affected: Workers and Anomalies keep their tiles
    await expect(page.getByTestId('workers-tile')).toBeVisible()
    await expect(page.getByRole('button', { name: /^Anomalies/ })).toBeVisible()
    // A part that cannot load has no chart controls to use
    await expect(page.getByRole('button', { name: 'Table', exact: true })).toHaveCount(0)
    const wrong = page.getByRole('region', { name: 'What’s wrong' })
    await expect(wrong).toContainText("Couldn't load the failures.")
    await expect(wrong.getByRole('table')).toHaveCount(0)
    await expect(page.getByRole('table', { name: 'Task health by group' })).toHaveCount(0)
    await expect(page.getByRole('region', { name: 'Workers' })).toContainText(
      "Couldn't load the workers.",
    )
    await expect(subtitle(page)).toHaveText('Last 6h')
    // Nothing loaded, so there is nothing stale to show
    await expect(staleLine(page)).toHaveCount(0)

    state.failing = false
    await page.getByRole('region', { name: 'Runs' }).getByRole('button', { name: 'Retry' }).click()
    await expect(page.getByTestId('chart-bar')).toHaveCount(12)
    await expect(page.getByTestId('runs-chart')).not.toContainText("Couldn't load")
  })

  test('Retry on a failed first load keeps the box, now "Retrying…", until data arrives', async ({
    page,
  }) => {
    let state = { failing: true }
    await open(page, async (p) => {
      state = await failSummaryEndpoints(p, { endpoints: [SERIES_ENDPOINT] })
    })
    const chart = page.getByRole('region', { name: 'Runs' })
    await expect(chart.getByRole('alert')).toBeVisible(AFTER_RETRIES)
    const box = await chart.getByRole('alert').elementHandle()

    state.failing = false
    await delayEndpoints(page, [SERIES_ENDPOINT], 1500)
    await chart.getByRole('button', { name: 'Retry' }).click()
    const retrying = chart.getByRole('button', { name: 'Retrying…' })
    await expect(retrying).toHaveAttribute('aria-disabled', 'true')
    // Never back to loading: the same alert node is still there
    await expect(chart).not.toContainText('Loading runs…')
    expect(await box?.evaluate((node) => node.isConnected)).toBe(true)
    await expect(page.getByTestId('chart-bar')).toHaveCount(12)
  })

  test('Retry that fails again leaves focus on the Retry button of the same box', async ({
    page,
  }) => {
    await open(page, (p) => failSummaryEndpoints(p, { endpoints: [SERIES_ENDPOINT] }))
    const chart = page.getByRole('region', { name: 'Runs' })
    await expect(chart.getByRole('alert')).toBeVisible(AFTER_RETRIES)
    const box = await chart.getByRole('alert').elementHandle()

    await chart.getByRole('button', { name: 'Retry' }).click()
    await expect(chart.getByRole('button', { name: 'Retrying…' })).toBeVisible()
    await expect(chart.getByRole('button', { name: 'Retry', exact: true })).toBeFocused(
      AFTER_RETRIES,
    )
    expect(await box?.evaluate((node) => node.isConnected)).toBe(true)
  })

  test('a part that never loaded keeps one alert across polls, with no flip to loading', async ({
    page,
  }) => {
    await open(page, (p) => failSummaryEndpoints(p), { clock: true })
    // 1 s, 2 s and 4 s of retries on the fake clock, then the error shows
    await advance(page, 8_000)
    await expect(page.getByRole('alert')).toHaveCount(5)
    const boxes = await page.getByRole('alert').elementHandles()

    // Two polls (15 s apart), each retried: sample every second of fake time
    for (let second = 0; second < 40; second++) {
      await advance(page, 1_000)
      await expect(page.getByRole('alert')).toHaveCount(5)
      await expect(page.getByText(/^Loading /)).toHaveCount(0)
      for (const box of boxes) expect(await box.evaluate((node) => node.isConnected)).toBe(true)
    }
  })

  test('a network failure says no response, not a status', async ({ page }) => {
    await open(page, (p) => failSummaryEndpoints(p, { mode: 'network' }))

    const wrong = page.getByRole('region', { name: 'What’s wrong' })
    await expect(wrong).toContainText("Couldn't load the failures.", AFTER_RETRIES)
    await expect(wrong).toContainText('No response from the server.')
    await expect(wrong).not.toContainText('The server answered')
  })

  test('401 with form login renders nothing: no box, no empty wrapper', async ({ page }) => {
    await open(page, (p) => failSummaryEndpoints(p, { mode: 'unauthorized' }), { loginPath: true })

    await expect(page.getByRole('region', { name: 'What’s wrong' })).toBeVisible()
    await page.waitForLoadState('networkidle')
    await expect(page.getByRole('alert')).toHaveCount(0)
    await expect(page.getByText("Couldn't load")).toHaveCount(0)
    await expect(page.getByText('valid API key')).toHaveCount(0)
    await expect(page.getByTestId('overview-error')).toHaveCount(0)
    await expect(staleLine(page)).toHaveCount(0)
  })

  test('401 without form login asks for a valid API key, with no Retry', async ({ page }) => {
    await open(page, (p) => failSummaryEndpoints(p, { mode: 'unauthorized' }))

    const wrong = page.getByRole('region', { name: 'What’s wrong' })
    await expect(wrong).toContainText('The server needs a valid API key.')
    await expect(wrong.getByRole('button', { name: 'Retry' })).toHaveCount(0)
    await expect(page.getByText("Couldn't load")).toHaveCount(0)
  })

  test('a server with no runs or workers says so plainly, not as an error', async ({ page }) => {
    await open(page, emptySummaryEndpoints)

    await expect(page.getByTestId('runs-chart')).toContainText('No runs in this window.')
    await expect(page.getByRole('alert')).toHaveCount(0)
    await expect(page.getByRole('region', { name: 'What’s wrong' })).toContainText(
      'All clear: no failures or anomalies in the last 6h.',
    )
    await expect(page.getByRole('table')).toHaveCount(0)
    const tile = page.getByTestId('workers-tile')
    await expect(tile).toContainText('No workers registered')
    await expect(tile).not.toContainText('All online')
    await expect(tile).not.toContainText('0/0')
    // The server sends 0.0 for no runs
    await expect(page.getByRole('button', { name: /^Failure rate/ })).toContainText('0%')
    await expect(subtitle(page)).toHaveText('Last 6h, ending 12:00 UTC')
  })

  test('while requests are in flight, each part is neither an error nor empty', async ({
    page,
  }) => {
    await open(page, (p) =>
      delayEndpoints(
        p,
        [SERIES_ENDPOINT, FAILURES_ENDPOINT, STATS_ENDPOINT, WORKERS_ENDPOINT],
        1500,
      ),
    )

    await expect(page.getByTestId('runs-chart')).toContainText('Loading runs…')
    await expect(page.getByTestId('runs-chart')).not.toContainText('No runs in this window.')
    await expect(page.getByRole('button', { name: 'Table', exact: true })).toHaveCount(0)
    await expect(page.getByText('Loading failures…')).toBeVisible()
    await expect(page.getByText('Loading task health…')).toBeVisible()
    await expect(page.getByText('Loading workers…')).toBeVisible()
    await expect(page.getByRole('alert')).toHaveCount(0)
    await expect(page.getByTestId('chart-bar')).toHaveCount(12)
  })

  // Every query the page renders, failing alone after good data. The fleet query (enabled only
  // when the anomaly list is capped) has no mock data; it is in the same list as these.
  const QUERIES = [
    { name: 'overview', endpoint: OVERVIEW_ENDPOINT },
    { name: 'series', endpoint: SERIES_ENDPOINT },
    { name: 'group series', endpoint: GROUP_SERIES_ENDPOINT },
    { name: 'failures', endpoint: FAILURES_ENDPOINT },
    { name: 'task stats', endpoint: STATS_ENDPOINT },
    { name: 'workers', endpoint: WORKERS_ENDPOINT },
    { name: 'anomalies', endpoint: ANOMALIES_ENDPOINT },
  ]
  for (const { name, endpoint } of QUERIES) {
    test(`the ${name} query failing alone after good data keeps the data, shows the stale line, and Retry refetches it`, async ({
      page,
    }) => {
      await open(page, async () => {}, { clock: true })
      await expect(page.getByTestId('chart-bar')).toHaveCount(12)
      await expect(page.getByRole('table', { name: 'Failures by exception' })).toBeVisible()
      await expect(page.getByRole('table', { name: 'Task health by group' })).toBeVisible()
      await expect(page.getByTestId('worker-strip')).toBeVisible()
      await expect(staleLine(page)).toHaveCount(0)

      const state = await failSummaryEndpoints(page, { endpoints: [endpoint] })
      await pollAndRetry(page)

      await expect(staleLine(page)).toContainText(
        "Couldn't refresh. Showing data from 12:00 UTC.",
        AFTER_RETRIES,
      )
      await expect(page.getByRole('alert')).toHaveCount(0)
      await expect(page.getByText(/^Loading /)).toHaveCount(0)
      await expect(page.getByTestId('chart-bar')).toHaveCount(12)
      await expect(page.getByRole('table', { name: 'Failures by exception' })).toBeVisible()
      await expect(page.getByRole('table', { name: 'Task health by group' })).toBeVisible()
      await expect(page.getByTestId('worker-strip')).toBeVisible()
      await expect(subtitle(page)).toContainText('ending')

      state.failing = false
      await delayEndpoints(page, [endpoint], 1000)
      await staleLine(page).getByRole('button', { name: 'Retry' }).click()
      await expect(staleLine(page).getByRole('button', { name: 'Retrying…' })).toHaveAttribute(
        'aria-disabled',
        'true',
      )
      await expect(staleLine(page)).toHaveCount(0)
      await expect(page.getByTestId('chart-bar')).toHaveCount(12)
    })
  }

  test('the stale time is the oldest failed query last success', async ({ page }) => {
    await open(page, async () => {}, { clock: true })
    await expect(page.getByTestId('chart-bar')).toHaveCount(12)

    // The series fails from the first poll on; the others keep refreshing for 2.5 minutes
    await failSummaryEndpoints(page, { endpoints: [SERIES_ENDPOINT] })
    await advance(page, 150_000)
    await expect(staleLine(page)).toContainText('from 12:00 UTC', AFTER_RETRIES)

    // Then the failures fail too, their last success a minute or more later than the series'
    await failSummaryEndpoints(page, { endpoints: [FAILURES_ENDPOINT] })
    await advance(page, 70_000)
    await expect(staleLine(page)).toContainText('from 12:00 UTC', AFTER_RETRIES)
    await expect(staleLine(page)).not.toContainText(/from 12:0[1-9]/)
    await expect(staleLine(page)).toHaveCount(1)
  })

  test('"All clear" goes away while the failures keep failing', async ({ page }) => {
    await open(page, emptySummaryEndpoints, { clock: true })
    const wrong = page.getByRole('region', { name: 'What’s wrong' })
    await expect(wrong).toContainText('All clear: no failures or anomalies in the last 6h.')
    await expect(wrong).toContainText('0 failing exceptions in the last 6h.')

    await failSummaryEndpoints(page, { endpoints: [FAILURES_ENDPOINT] })
    await pollAndRetry(page)

    await expect(staleLine(page)).toBeVisible(AFTER_RETRIES)
    await expect(wrong).not.toContainText('All clear')
    await expect(wrong).not.toContainText('0 failing exceptions')
  })

  test('"All clear" goes away while the anomalies keep failing, and the page says why', async ({
    page,
  }) => {
    await open(page, emptySummaryEndpoints, { clock: true })
    const wrong = page.getByRole('region', { name: 'What’s wrong' })
    await expect(wrong).toContainText('All clear: no failures or anomalies in the last 6h.')

    await failSummaryEndpoints(page, { endpoints: [ANOMALIES_ENDPOINT] })
    await pollAndRetry(page)

    await expect(staleLine(page)).toBeVisible(AFTER_RETRIES)
    await expect(wrong).not.toContainText('All clear')
    await expect(wrong).not.toContainText('0 failing exceptions')
  })
})
