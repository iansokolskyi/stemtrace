import { expect, type Page, test } from '@playwright/test'

import { setupMockApi } from './fixtures/mock-api'
import { mockOverview, spikeScenario, spikeTasks } from './fixtures/overview'

/**
 * The type scale and weight roles (restyle PR 3): every page title is one `PageHeader` at
 * 22 px / 800, section heads are 15 px / 700 and sub-headings are muted 12 px / 600. Mock mode
 * only: the Overview numbers come from `fixtures/overview.ts`.
 */

test.skip(process.env.E2E_MODE === 'real', 'Mock-mode fixtures')

const PAGES = ['/', '/executions', '/workers', '/anomalies', '/registry', '/flows']

async function open(page: Page, path: string): Promise<void> {
  const api = await setupMockApi(page)
  api.clearTasks()
  const scenario = spikeScenario()
  api.addTasks(spikeTasks(scenario).tasks)
  await mockOverview(page, scenario)
  await page.goto(path)
}

for (const path of PAGES) {
  test(`page title on ${path} is 22 px, weight 800, tracked -0.015em`, async ({ page }) => {
    await open(page, path)
    const title = page.getByRole('heading', { level: 1 })
    await expect(title).toHaveCSS('font-size', '22px')
    await expect(title).toHaveCSS('font-weight', '800')
    await expect(title).toHaveCSS('letter-spacing', '-0.33px')
  })
}

test.describe('Overview heading levels', () => {
  test('section head, sub-heading and exception class differ in level', async ({ page }) => {
    await open(page, '/')
    const wrong = page.getByRole('heading', { name: 'What’s wrong', level: 2 })
    await expect(wrong).toHaveCSS('font-size', '15px')
    await expect(wrong).toHaveCSS('font-weight', '700')

    const sub = page.getByRole('heading', { name: 'Failures by exception', level: 3 })
    await expect(sub).toHaveCSS('font-size', '12px')
    await expect(sub).toHaveCSS('font-weight', '600')

    const table = page.getByRole('table', { name: 'Failures by exception' })
    const exception = table.getByRole('button').first()
    await expect(exception).toHaveCSS('font-family', /Overpass Variable/)
    await expect(exception).toHaveCSS('font-weight', '600')
    await expect(exception).not.toHaveCSS('font-family', /Mono/)
  })
})
