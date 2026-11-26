import { expect, type Page, test } from '@playwright/test'

import { setupMockApi } from './fixtures/mock-api'
import { mockOverview, spikeScenario } from './fixtures/overview'
import {
  delayEndpoints,
  emptySummaryEndpoints,
  FAILURES_ENDPOINT,
  failSummaryEndpoints,
  SERIES_ENDPOINT,
  SUMMARY_ENDPOINTS,
} from './fixtures/overview-states'

/**
 * The Overview's loading, error and empty states (ui-restyle-audit G3, readings O1-O6). Mock mode
 * only. An error must not read as "no data", a window with no runs must not read as a failure, and
 * one failed poll must not wipe data that loaded before it.
 */

const NOW = new Date('2026-10-17T12:00:00Z')
// A first load that fails is retried 3 times (1 s, 2 s, 4 s) before its box shows
const AFTER_RETRIES = { timeout: 20_000 }

async function open(
  page: Page,
  shape: (page: Page) => Promise<unknown>,
  { clock = false }: { clock?: boolean } = {},
): Promise<void> {
  await page.routeWebSocket(/\/ws$/, () => {})
  if (clock) await page.clock.install({ time: NOW })
  const api = await setupMockApi(page)
  api.clearTasks()
  await mockOverview(page, spikeScenario(NOW.getTime()))
  await shape(page)
  await page.goto('/')
  await expect(page.getByRole('heading', { level: 1, name: 'Overview' })).toBeVisible()
}

const subtitle = (page: Page) => page.getByRole('heading', { level: 1 }).locator('xpath=../p')
const staleLine = (page: Page) => page.getByRole('status').filter({ hasText: "Couldn't refresh." })

/** The clock runs past the next poll (15 s) and the retries that follow a failed one (7 s). */
async function pollAndRetry(page: Page): Promise<void> {
  await page.clock.runFor(16_000)
  await page.clock.runFor(10_000)
}

test.describe('Overview states', () => {
  test.skip(process.env.E2E_MODE === 'real', 'mock-only: the endpoints are made to fail or empty')
  test.setTimeout(60_000)

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

  test('Retry on a failed first load shows the part loading again', async ({ page }) => {
    let state = { failing: true }
    await open(page, async (p) => {
      state = await failSummaryEndpoints(p, { endpoints: [SERIES_ENDPOINT] })
    })
    const chart = page.getByRole('region', { name: 'Runs' })
    await expect(chart.getByRole('alert')).toBeVisible()

    state.failing = false
    await delayEndpoints(page, [SERIES_ENDPOINT], 1500)
    await chart.getByRole('button', { name: 'Retry' }).click()
    await expect(chart).toContainText('Loading runs…')
    await expect(chart.getByRole('alert')).toHaveCount(0)
    await expect(page.getByTestId('chart-bar')).toHaveCount(12)
  })

  test('a failed first load without a response says no response, not a status', async ({
    page,
  }) => {
    await open(page, (p) => failSummaryEndpoints(p, { mode: 'network' }))

    const wrong = page.getByRole('region', { name: 'What’s wrong' })
    await expect(wrong).toContainText("Couldn't load the failures.", AFTER_RETRIES)
    await expect(wrong).toContainText('No response from the server.')
    await expect(wrong).not.toContainText('The server answered')
  })

  test('an expired session gets no error box: the login redirect handles it', async ({ page }) => {
    await open(page, (p) => failSummaryEndpoints(p, { mode: 'unauthorized' }))

    await expect(page.getByRole('region', { name: 'What’s wrong' })).toBeVisible()
    await page.waitForLoadState('networkidle')
    await expect(page.getByRole('alert')).toHaveCount(0)
    await expect(page.getByText("Couldn't load")).toHaveCount(0)
    await expect(staleLine(page)).toHaveCount(0)
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
        [SERIES_ENDPOINT, FAILURES_ENDPOINT, SUMMARY_ENDPOINTS[3], SUMMARY_ENDPOINTS[4]],
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

  test('a failed poll keeps the data and says it is stale, then recovers', async ({ page }) => {
    await open(page, async () => {}, { clock: true })
    await expect(page.getByTestId('chart-bar')).toHaveCount(12)
    await expect(page.getByRole('table', { name: 'Failures by exception' })).toBeVisible()

    const state = await failSummaryEndpoints(page)
    await pollAndRetry(page)

    await expect(staleLine(page)).toHaveText(
      /Couldn't refresh\. Showing data from 12:00 UTC\./,
      AFTER_RETRIES,
    )
    await expect(page.getByRole('alert')).toHaveCount(0)
    await expect(page.getByTestId('chart-bar')).toHaveCount(12)
    await expect(page.getByRole('table', { name: 'Failures by exception' })).toBeVisible()
    await expect(page.getByRole('table', { name: 'Task health by group' })).toBeVisible()
    await expect(page.getByTestId('worker-strip')).toBeVisible()
    await expect(page.getByRole('button', { name: /^Runs \/ min/ })).not.toHaveText(/^Runs \/ min$/)
    await expect(subtitle(page)).toContainText('ending')

    state.failing = false
    await delayEndpoints(page, SUMMARY_ENDPOINTS, 1000)
    await staleLine(page).getByRole('button', { name: 'Retry' }).click()
    await expect(staleLine(page).getByRole('button', { name: 'Retrying…' })).toBeDisabled()
    await expect(staleLine(page)).toHaveCount(0)
    // The button went with the line: focus is on the page heading, not lost on the body
    await expect(page.getByRole('heading', { level: 1 })).toBeFocused()
    await expect(page.getByTestId('chart-bar')).toHaveCount(12)
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
})
