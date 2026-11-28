import { expect, type Page, test } from '@playwright/test'

import { flowNode } from './fixtures/flow'
import { reallocationFlow, serveFlow } from './fixtures/flow-graphs'
import { setupMockApi } from './fixtures/mock-api'
import { mockOverview, spikeScenario, spikeTasks } from './fixtures/overview'
import { openTask, successTask, tokenColour } from './fixtures/task-page'

/**
 * The visual system (restyle PR 4): success bars and blocks use the quiet fill while failure
 * keeps full strength, and every table header sits on the tinted band. Mock mode only.
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

test.describe('Quiet success fills', () => {
  test('runs chart: success bars are quiet, failure bars and legend keep their colours', async ({
    page,
  }) => {
    await openOverview(page)
    const quiet = await tokenColour(page, 'success-quiet')
    const failure = await tokenColour(page, 'failure')
    const fills = await page
      .getByTestId('chart-bar')
      .locator('rect')
      .evaluateAll((rects) => rects.map((r) => getComputedStyle(r).fill))
    expect(fills).toContain(quiet)
    expect(fills).toContain(failure)
    // Each success segment has a edge in the text-safe success tint, 1.5 px tall (V1a).
    const success = await tokenColour(page, 'success-t')
    const edge = page.getByTestId('chart-bar').first().locator('rect').nth(1)
    await expect(edge).toHaveCSS('fill', success)
    expect((await edge.boundingBox())?.height).toBeCloseTo(1.5, 1)
  })

  test('flow diagram: a successful block is quiet, a failed block is full failure', async ({
    page,
  }) => {
    await openFlow(page)
    await expect(flowNode(page, 'root').locator('.blk')).toHaveCSS(
      'fill',
      await tokenColour(page, 'success-quiet'),
    )
    await expect(flowNode(page, 'p2').locator('.blk')).toHaveCSS(
      'fill',
      await tokenColour(page, 'failure'),
    )
    // The quiet block has a full-strength 1.5 px outline; the failed block has none (V1a).
    const root = flowNode(page, 'root').locator('.blk')
    await expect(root).toHaveCSS('stroke', await tokenColour(page, 'success-t'))
    await expect(root).toHaveCSS('stroke-width', '1.5px')
    await expect(flowNode(page, 'p2').locator('.blk')).toHaveCSS('stroke', 'none')
  })

  test('flow timeline: success marks are quiet, failure marks are full failure', async ({
    page,
  }) => {
    await openFlow(page)
    const quiet = await tokenColour(page, 'success-quiet')
    const failure = await tokenColour(page, 'failure')
    // Small marks keep the full success colour (V2); the bar is the large area.
    await expect(page.locator('.gantt-row.s-success .gantt-mark').first()).toHaveCSS(
      'fill',
      await tokenColour(page, 'success'),
    )
    await expect(page.locator('.gantt-row.s-failure .gantt-mark').first()).toHaveCSS(
      'fill',
      failure,
    )
    const run = page.locator('.gantt-row.s-success .gantt-run:not(.gantt-crit)').first()
    await expect(run).toHaveCSS('fill', quiet)
    await expect(run).toHaveCSS('stroke', await tokenColour(page, 'success-t'))
  })

  test('task timing bar: a successful run is quiet, a failed run is full failure', async ({
    page,
  }) => {
    await openTask(page, successTask({ queue_wait_ms: 1000, runtime_ms: 2000 }))
    const bar = page.getByRole('region', { name: 'Timing' }).getByTestId('timing-runtime')
    await expect(bar).toHaveCSS('background-color', await tokenColour(page, 'success-quiet'))
    // Edge: an inset 1.5 px shadow in the full success colour.
    const shadow = await bar.evaluate((el) => getComputedStyle(el).boxShadow)
    expect(shadow).toContain(await tokenColour(page, 'success-t'))
    expect(shadow).toContain('1.5px')
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
