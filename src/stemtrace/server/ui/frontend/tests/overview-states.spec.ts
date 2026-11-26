import { expect, type Page, test } from '@playwright/test'

import { setupMockApi } from './fixtures/mock-api'
import { mockOverview, spikeScenario } from './fixtures/overview'
import { emptySummaryEndpoints, failSummaryEndpoints } from './fixtures/overview-states'

/**
 * The Overview's loading, error and empty states (ui-restyle-audit G3). Mock mode only. An error
 * must not read as "no data", and a window with no runs must not read as a failure.
 */

async function open(page: Page, shape: (page: Page) => Promise<unknown>): Promise<void> {
  await page.routeWebSocket(/\/ws$/, () => {})
  const api = await setupMockApi(page)
  api.clearTasks()
  await mockOverview(page, spikeScenario())
  await shape(page)
  await page.goto('/')
  await expect(page.getByRole('heading', { level: 1, name: 'Overview' })).toBeVisible()
}

const subtitle = (page: Page) => page.getByRole('heading', { level: 1 }).locator('xpath=../p')

test.describe('Overview states', () => {
  test.skip(process.env.E2E_MODE === 'real', 'mock-only: the endpoints are made to fail or empty')

  test('failed requests say so in every part and offer Retry', async ({ page }) => {
    let state = { failing: true }
    await open(page, async (p) => {
      state = await failSummaryEndpoints(p)
    })

    await expect(
      page.getByRole('alert').filter({ hasText: "Couldn't load the overview." }),
    ).toBeVisible()
    await expect(page.getByRole('alert')).toHaveCount(5)
    await expect(page.getByTestId('runs-chart')).toContainText("Couldn't load the runs.")
    await expect(page.getByTestId('runs-chart')).not.toContainText('No runs in this window.')
    await expect(page.getByTestId('runs-chart')).toContainText('The server answered 500.')
    const wrong = page.getByRole('region', { name: 'What’s wrong' })
    await expect(wrong).toContainText("Couldn't load the failures.")
    await expect(wrong.getByRole('table')).toHaveCount(0)
    await expect(page.getByRole('table', { name: 'Task health by group' })).toHaveCount(0)
    await expect(page.getByRole('region', { name: 'Workers' })).toContainText(
      "Couldn't load the workers.",
    )
    await expect(subtitle(page)).toHaveText('Last 6h')

    state.failing = false
    await page.getByRole('region', { name: 'Runs' }).getByRole('button', { name: 'Retry' }).click()
    await expect(page.getByTestId('chart-bar')).toHaveCount(12)
    await expect(page.getByTestId('runs-chart')).not.toContainText("Couldn't load")
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
    await expect(subtitle(page)).toHaveText('Last 6h, ending 12:00 UTC')
  })

  test('while the series loads, the chart is neither an error nor empty', async ({ page }) => {
    await page.routeWebSocket(/\/ws$/, () => {})
    const api = await setupMockApi(page)
    api.clearTasks()
    const scenario = spikeScenario()
    scenario.seriesDelayMs = 1500
    await mockOverview(page, scenario)
    await page.goto('/')
    await expect(page.getByRole('heading', { level: 1, name: 'Overview' })).toBeVisible()

    await expect(page.getByTestId('runs-chart')).toContainText('Loading runs…')
    await expect(page.getByTestId('runs-chart')).not.toContainText('No runs in this window.')
    await expect(page.getByRole('alert')).toHaveCount(0)
    await expect(page.getByTestId('chart-bar')).toHaveCount(12)
  })
})
