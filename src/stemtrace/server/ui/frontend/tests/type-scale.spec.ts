import { expect, type Locator, type Page, test } from '@playwright/test'

import { reallocationFlow, serveFlow } from './fixtures/flow-graphs'
import { setupMockApi } from './fixtures/mock-api'
import { createFailedTask } from './fixtures/mock-data'
import { mockOverview, spikeScenario, spikeTasks } from './fixtures/overview'
import { openTask, successTask, tokenColour } from './fixtures/task-page'

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

async function expectTitleStyle(page: Page): Promise<void> {
  const title = page.getByRole('heading', { level: 1 })
  await expect(title).toHaveCSS('font-size', '22px')
  await expect(title).toHaveCSS('font-weight', '800')
  await expect(title).toHaveCSS('letter-spacing', '-0.33px')
}

async function expectSummaryOnTitleRow(page: Page, summary: Locator): Promise<void> {
  await expect(summary).toBeVisible()
  const title = await page.getByRole('heading', { level: 1 }).boundingBox()
  const box = await summary.boundingBox()
  const mid = (box?.y ?? 0) + (box?.height ?? 0) / 2
  expect(mid).toBeGreaterThanOrEqual(title?.y ?? 0)
  expect(mid).toBeLessThanOrEqual((title?.y ?? 0) + (title?.height ?? 0))
}

for (const path of PAGES) {
  test(`page title on ${path} is 22 px, weight 800, tracked -0.015em`, async ({ page }) => {
    await open(page, path)
    await expectTitleStyle(page)
  })

  test(`summary shares the title row on ${path} at 1280`, async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 900 })
    await open(page, path)
    await expectSummaryOnTitleRow(
      page,
      page.getByRole('heading', { level: 1 }).locator('xpath=following-sibling::p'),
    )
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

test.describe('Header variants', () => {
  test('Task page title (stacked header) is 22 px / 800 and sits under the module line', async ({
    page,
  }) => {
    await openTask(page, successTask())
    await expectTitleStyle(page)
    const module = await page.locator('[data-task-module]').boundingBox()
    const title = await page.getByRole('heading', { level: 1 }).boundingBox()
    expect((module?.y ?? 0) + (module?.height ?? 0)).toBeLessThanOrEqual(title?.y ?? 0)
  })

  test('Task page exception class is ink, sans, 600 (not failure red or mono)', async ({
    page,
  }) => {
    await openTask(
      page,
      createFailedTask('billing.payments.capture', 'GatewayTimeout: no answer', 'Traceback'),
    )
    const exception = page.getByRole('region', { name: 'Exception' }).getByText('GatewayTimeout', {
      exact: true,
    })
    await expect(exception).toHaveCSS('font-weight', '600')
    await expect(exception).toHaveCSS('font-family', /Overpass Variable/)
    await expect(exception).not.toHaveCSS('font-family', /Mono/)
    await expect(exception).toHaveCSS('color', await tokenColour(page, 'ink'))
  })

  test('Flow page title is 22 px / 800 with its summary on the title row', async ({ page }) => {
    const graph = reallocationFlow()
    await setupMockApi(page, { useDefaults: false })
    await serveFlow(page, graph, {})
    await page.setViewportSize({ width: 1280, height: 900 })
    await page.goto(`/flows/${encodeURIComponent(graph.root_id)}`)
    await expectTitleStyle(page)
    await expectSummaryOnTitleRow(
      page,
      page.getByRole('heading', { level: 1 }).locator('xpath=following-sibling::p'),
    )
  })

  test('Worker page title is 22 px / 800 and set in mono', async ({ page }) => {
    const api = await setupMockApi(page, { useDefaults: false })
    api.setWorkers([
      {
        hostname: 'celery@alpha',
        pid: 1,
        registered_tasks: [],
        status: 'online',
        registered_at: new Date().toISOString(),
        last_seen: new Date().toISOString(),
      },
    ])
    await page.goto('/workers/celery%40alpha')
    await expectTitleStyle(page)
    await expect(page.getByRole('heading', { level: 1 })).toHaveCSS('font-family', /Mono/)
  })

  test('Registry header keeps its height when the summary arrives, at 400 px', async ({ page }) => {
    await page.setViewportSize({ width: 400, height: 800 })
    let release = (): void => {}
    const gate = new Promise<void>((resolve) => {
      release = resolve
    })
    await setupMockApi(page)
    await page.route('**/api/tasks/registry*', async (route) => {
      await gate
      await route.fallback()
    })
    await page.goto('/registry')
    const header = page.getByRole('heading', { level: 1 }).locator('..')
    await expect(page.getByRole('heading', { level: 1 })).toBeVisible()
    const before = (await header.boundingBox())?.height ?? 0
    release()
    await expect(header.locator('p')).toContainText('task name')
    const after = (await header.boundingBox())?.height ?? 0
    expect(after).toBe(before)
  })
})
