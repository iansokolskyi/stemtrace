import { expect, type Page, test } from '@playwright/test'

import { flowNode } from './fixtures/flow'
import { reallocationFlow, serveFlow } from './fixtures/flow-graphs'
import { setupMockApi } from './fixtures/mock-api'
import { mockOverview, spikeScenario, spikeTasks } from './fixtures/overview'
import { openTask, successTask, tokenColour } from './fixtures/task-page'

/**
 * The visual system (restyle PR 4): success and failure bars and blocks are solid fills with no
 * edge, as in the approved mockup, and every table header sits on the tinted band. Mock mode only.
 */

test.skip(process.env.E2E_MODE === 'real', 'Mock-only test')

async function openOverview(page: Page): Promise<void> {
  const api = await setupMockApi(page)
  api.clearTasks()
  const scenario = spikeScenario()
  api.addTasks(spikeTasks(scenario).tasks)
  await mockOverview(page, scenario)
  await page.goto('/')
  await expect(page.getByTestId('chart-bar')).toHaveCount(12)
}

async function openFlow(page: Page): Promise<void> {
  const graph = reallocationFlow()
  await page.setViewportSize({ width: 1280, height: 900 })
  await setupMockApi(page, { useDefaults: false })
  await serveFlow(page, graph)
  await page.routeWebSocket(/\/ws$/, (ws) => ws.close())
  await page.goto(`/flows/${encodeURIComponent(graph.root_id)}`)
  await flowNode(page, 'root').waitFor({ state: 'attached' })
}

test.describe('Solid state fills', () => {
  test('runs chart: success and failure bars are solid, with no edge', async ({ page }) => {
    await openOverview(page)
    const success = await tokenColour(page, 'success')
    const failure = await tokenColour(page, 'failure')
    const fills = await page
      .getByTestId('chart-bar')
      .locator('rect')
      .evaluateAll((rects) => rects.map((r) => getComputedStyle(r).fill))
    expect(fills).toContain(success)
    expect(fills).toContain(failure)
    // One rect per segment: a quiet bar (40 runs, all success) has no extra edge rect.
    await expect(page.getByTestId('chart-bar').first().locator('rect')).toHaveCount(1)
  })

  test('flow diagram: a successful block is solid success, a failed block solid failure', async ({
    page,
  }) => {
    await openFlow(page)
    const root = flowNode(page, 'root').locator('.blk')
    await expect(root).toHaveCSS('fill', await tokenColour(page, 'success'))
    await expect(root).toHaveCSS('stroke', 'none')
    await expect(flowNode(page, 'p2').locator('.blk')).toHaveCSS(
      'fill',
      await tokenColour(page, 'failure'),
    )
  })

  test('flow timeline: success bars and marks are solid success', async ({ page }) => {
    await openFlow(page)
    const success = await tokenColour(page, 'success')
    await expect(page.locator('.gantt-row.s-success .gantt-mark').first()).toHaveCSS(
      'fill',
      success,
    )
    const run = page.locator('.gantt-row.s-success .gantt-run:not(.gantt-crit)').first()
    await expect(run).toHaveCSS('fill', success)
    await expect(run).toHaveCSS('stroke', 'none')
    await expect(page.locator('.gantt-row.s-failure .gantt-mark').first()).toHaveCSS(
      'fill',
      await tokenColour(page, 'failure'),
    )
  })

  test('task timing bar: a successful run is solid success with no edge', async ({ page }) => {
    await openTask(page, successTask({ queue_wait_ms: 1000, runtime_ms: 2000 }))
    const bar = page.getByRole('region', { name: 'Timing' }).getByTestId('timing-runtime')
    await expect(bar).toHaveCSS('background-color', await tokenColour(page, 'success'))
    await expect(bar).toHaveCSS('box-shadow', 'none')
  })

  test('task timing bar: a failed run is full failure', async ({ page }) => {
    await openTask(page, successTask({ state: 'FAILURE', queue_wait_ms: 1000, runtime_ms: 2000 }))
    const bar = page.getByRole('region', { name: 'Timing' }).getByTestId('timing-runtime')
    await expect(bar).toHaveCSS('background-color', await tokenColour(page, 'failure'))
  })
})

test.describe('Header band', () => {
  const PAGES: [string, string][] = [
    ['/', 'Exception'],
    ['/registry', 'Task'],
    ['/workers', 'Hostname'],
  ]

  test('the Executions header row sits on the band between two rules', async ({ page }) => {
    await setupMockApi(page)
    await page.goto('/executions')
    const row = page.locator('[data-column-header]').first().locator('xpath=..')
    await expect(row).toBeVisible()
    await expect(row).toHaveCSS('background-color', await tokenColour(page, 'band'))
    await expect(row).toHaveCSS('border-top-width', '1px')
    await expect(row).toHaveCSS('border-bottom-width', '1px')
    await expect(row).toHaveCSS('font-weight', '500')
  })

  for (const [path, column] of PAGES) {
    test(`the ${column} column header on ${path} sits on the band between two rules`, async ({
      page,
    }) => {
      if (path === '/') await openOverview(page)
      else {
        await setupMockApi(page)
        await page.goto(path)
      }
      const header = page.getByRole('columnheader', { name: column, exact: true }).first()
      await expect(header).toBeVisible()
      await expect(header).toHaveCSS('background-color', await tokenColour(page, 'band'))
      await expect(header).toHaveCSS('border-top-width', '1px')
      await expect(header).toHaveCSS('border-bottom-width', '1px')
      await expect(header).toHaveCSS('font-weight', '500')
    })
  }
})

test.describe('Sub-heading counts', () => {
  test('the count sits beside the heading, not in it, and the heading name stays the words', async ({
    page,
  }) => {
    await openOverview(page)
    await expect(page.getByRole('heading', { name: 'Failures by exception', level: 3 })).toHaveText(
      'Failures by exception',
    )
    await expect(page.getByTestId('failures-count')).toHaveText('3')
    await expect(page.getByTestId('failures-count')).toHaveCSS(
      'color',
      await tokenColour(page, 'muted'),
    )
  })
})
