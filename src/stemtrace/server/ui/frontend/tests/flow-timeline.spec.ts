import { expect, type Locator, type Page, test } from '@playwright/test'

import { flowNode, flowNodes } from './fixtures/flow'
import {
  chain,
  type FlowGraph,
  group,
  node,
  reallocationFlow,
  serveFlow,
} from './fixtures/flow-graphs'
import { setupMockApi } from './fixtures/mock-api'

/**
 * The Timeline panel on the Flow page: rows, selection both ways, the "+N more" row, accessible
 * names, text size and the narrow screen. The bar rules themselves are in timeline.spec.ts.
 * Mock mode only.
 */

test.skip(process.env.E2E_MODE === 'real', 'Mock-only test')

const BASE = Date.UTC(2026, 0, 1)
const at = (ms: number): string => new Date(BASE + ms).toISOString()

async function openFlow(page: Page, graph: FlowGraph, width = 1280): Promise<void> {
  await page.setViewportSize({ width, height: 900 })
  await setupMockApi(page, { useDefaults: false })
  await serveFlow(page, graph)
  await page.routeWebSocket(/\/ws$/, (ws) => ws.close())
  await page.goto(`/flows/${encodeURIComponent(graph.root_id)}`)
  await flowNodes(page).first().waitFor({ state: 'attached' })
}

const canvas = (page: Page) => page.getByTestId('flow-canvas')
const scrollLeft = (page: Page) => canvas(page).evaluate((el) => el.scrollLeft)
const rows = (page: Page) => page.getByTestId('gantt-row')
const row = (page: Page, id: string): Locator =>
  page.locator(`[data-testid="gantt-row"][data-row-id="${id}"]`)
const timelinePanel = (page: Page) => page.getByRole('region', { name: 'Timeline' })

/** c0 -> ... -> c<length-1>, each starting when the one before ended: 100 ms wait, 900 ms run. */
function timedChain(length: number, failed: number[] = []): FlowGraph {
  const nodes = chain(length)
  for (const [i, entry] of Object.values(nodes).entries()) {
    entry.name = `app.step_${i}`
    entry.first_seen = at(i * 1000)
    entry.last_updated = at(i * 1000 + 1000)
    entry.queue_wait_ms = 100
    entry.runtime_ms = 900
    entry.total_runtime_ms = 900
  }
  for (const index of failed) nodes[`c${index}`].state = 'FAILURE'
  return { root_id: 'c0', nodes }
}

/** A chord of 20 whose slowest header, m14, is hidden behind "+N more"; with a callback. */
function slowHiddenChord(): FlowGraph {
  const nodes = group(20, {}, 'CHORD')
  nodes.g.chord_id = 'cb'
  nodes.g.children.push('cb')
  for (const [i, entry] of Object.values(nodes).entries()) {
    if (entry.task_id === 'g') continue
    const index = Number(entry.task_id.slice(1))
    const end = 2000 + (index === 14 ? 3000 : index * 10)
    entry.first_seen = at(1000)
    entry.last_updated = at(end)
    entry.queue_wait_ms = 0
    entry.runtime_ms = end - 1000
    entry.total_runtime_ms = end - 1000
    entry.name = `app.header_${i}`
  }
  nodes.cb = node({
    task_id: 'cb',
    name: 'app.callback',
    first_seen: at(5000),
    last_updated: at(5400),
    queue_wait_ms: 0,
    runtime_ms: 400,
    total_runtime_ms: 400,
  })
  return { root_id: 'g', nodes }
}

test.describe('Timeline rows', () => {
  test('has a row for every drawn task in layout order, with the axis start and ticks', async ({
    page,
  }) => {
    await openFlow(page, timedChain(4))

    await expect(rows(page)).toHaveCount(4)
    await expect(rows(page).first()).toHaveAttribute('data-row-id', 'c0')
    await expect(rows(page).last()).toHaveAttribute('data-row-id', 'c3')
    await expect(timelinePanel(page)).toContainText(/start \d\d:\d\d:\d\d/)
    await expect(timelinePanel(page)).toContainText('+1.0s')
    await expect(timelinePanel(page)).toContainText('+2.0s')
  })

  test('draws a lead-in for the queue wait, then the run, starting after its row start', async ({
    page,
  }) => {
    await openFlow(page, timedChain(2))
    // Attributes, not boxes: the critical outline widens a bar's box by its stroke
    const span = (locator: Locator) =>
      locator.evaluate((el) => ({
        x: Number(el.getAttribute('x')),
        right: Number(el.getAttribute('x')) + Number(el.getAttribute('width')),
      }))
    const wait = await span(row(page, 'c1').locator('.gantt-wait'))
    const run = await span(row(page, 'c1').locator('.gantt-run'))
    const first = await span(row(page, 'c0').locator('.gantt-run'))

    // the wait ends where the run begins; c1 starts running after c0 ended
    expect(wait.right).toBeCloseTo(run.x, 3)
    expect(run.x).toBeGreaterThan(first.right)
  })

  test('says "on the critical path" in the accessible name of the rows that are on it', async ({
    page,
  }) => {
    await openFlow(page, slowHiddenChord())

    await expect(row(page, 'g')).toHaveCount(1) // the "+N more" row, keyed by its container
    await expect(row(page, 'cb')).toHaveAttribute('aria-label', /, on the critical path$/)
    await expect(row(page, 'm0')).not.toHaveAttribute('aria-label', /critical/)
    await expect(row(page, 'g')).toHaveAttribute('aria-label', /^\+17 more: .*, on the critical path$/)
    await expect(row(page, 'g')).toHaveAttribute('data-critical', 'true')
    await expect(row(page, 'cb').getByText('critical', { exact: true })).toBeVisible()
  })

  test('names the state and the timing of a row', async ({ page }) => {
    await openFlow(page, timedChain(3, [2]))

    await expect(row(page, 'c1')).toHaveAttribute(
      'aria-label',
      'app.step_1, Success, waited 100ms, ran 900ms, on the critical path',
    )
    await expect(row(page, 'c2')).toHaveAttribute('aria-label', /^app\.step_2, Failure, /)
  })

  test('shows a callback that did not run as a dotted row that says so', async ({ page }) => {
    const flow = reallocationFlow()
    await openFlow(page, flow)
    const notRun = row(page, 'chord').and(page.locator('[data-row-kind="notRun"]'))

    await expect(notRun).toHaveAttribute(
      'aria-label',
      'Callback not run, Chord header failed: 1 of 48 tasks failed',
    )
    await expect(notRun).toContainText('not run')
    await expect(notRun.locator('.gantt-run')).toHaveCount(0)
  })

  test('writes every label at 12px or more', async ({ page }) => {
    await openFlow(page, reallocationFlow())
    await rows(page).first().waitFor()

    const sizes = await timelinePanel(page)
      .locator('text')
      .evaluateAll((els) => els.map((el) => Number.parseFloat(getComputedStyle(el).fontSize)))
    expect(sizes.length).toBeGreaterThan(8)
    expect(Math.min(...sizes)).toBeGreaterThanOrEqual(12)
  })

  test('keeps numbers in the sans font with tabular figures', async ({ page }) => {
    await openFlow(page, timedChain(3))

    const style = await timelinePanel(page)
      .locator('text', { hasText: /^\+1\.0s$/ })
      .evaluate((el) => {
        const css = getComputedStyle(el)
        return { variant: css.fontVariantNumeric, family: css.fontFamily }
      })
    expect(style.variant).toContain('tabular-nums')
    expect(style.family).not.toMatch(/mono/i)
  })
})

test.describe('choosing from the Timeline', () => {
  test('a row chooses its node and centres it in the diagram', async ({ page }) => {
    await openFlow(page, timedChain(10))
    await expect(flowNode(page, 'c9')).not.toBeInViewport()

    await row(page, 'c9').click()

    await expect(flowNode(page, 'c9')).toHaveAttribute('aria-pressed', 'true')
    await expect(flowNode(page, 'c9')).toBeInViewport()
    expect(await scrollLeft(page)).toBeGreaterThan(300)
    await expect(page.getByRole('complementary', { name: 'Inspector' })).toContainText('step_9')
    await expect(row(page, 'c9')).toHaveAttribute('aria-pressed', 'true')
  })

  for (const key of ['Enter', ' ']) {
    test(`a row is chosen with ${key === ' ' ? 'Space' : key}`, async ({ page }) => {
      await openFlow(page, timedChain(4))

      await row(page, 'c2').focus()
      await page.keyboard.press(key)

      await expect(flowNode(page, 'c2')).toHaveAttribute('aria-pressed', 'true')
    })
  }

  test('highlights the row of the node chosen in the diagram, and only that one', async ({
    page,
  }) => {
    await openFlow(page, timedChain(4))

    await flowNode(page, 'c2').click()

    await expect(row(page, 'c2')).toHaveAttribute('aria-pressed', 'true')
    await expect(row(page, 'c2')).toHaveClass(/\bsel\b/)
    await expect(page.locator('[data-testid="gantt-row"].sel')).toHaveCount(1)
  })

  test('highlights the first failure on open', async ({ page }) => {
    await openFlow(page, timedChain(5, [3]))

    await expect(row(page, 'c3')).toHaveAttribute('aria-pressed', 'true')
  })

  test('a "+N more" row expands its container, like the diagram block', async ({ page }) => {
    await openFlow(page, slowHiddenChord())
    // the first three headers, the "+17 more" row and the callback
    await expect(rows(page)).toHaveCount(5)

    await row(page, 'g').click()

    await expect(page.locator('[data-testid="flow-more"][data-kind="more"]')).toHaveCount(0)
    await expect(row(page, 'm14')).toHaveAttribute('aria-label', /on the critical path$/)
    await expect(row(page, 'm19')).toHaveCount(1)
    await expect(rows(page)).toHaveCount(20 + 1)
  })

  test('a "+N more" row opens from the keyboard', async ({ page }) => {
    await openFlow(page, slowHiddenChord())

    await row(page, 'g').focus()
    await page.keyboard.press('Enter')

    await expect(row(page, 'm14')).toHaveCount(1)
  })

  test('choosing a row locks the view: a live update does not move it', async ({ page }) => {
    const graph = timedChain(12)
    await setupMockApi(page, { useDefaults: false })
    await serveFlow(page, graph)
    await page.routeWebSocket(/\/ws$/, (ws) => ws.close())
    await page.setViewportSize({ width: 1280, height: 900 })
    await page.goto('/flows/c0')
    await flowNodes(page).first().waitFor({ state: 'attached' })

    await row(page, 'c11').click()
    await expect(flowNode(page, 'c11')).toBeInViewport()
    await expect
      .poll(async () => {
        const first = await scrollLeft(page)
        await page.waitForTimeout(300)
        return first === (await scrollLeft(page))
      })
      .toBe(true)
    const rested = await scrollLeft(page)

    graph.nodes.c0.children.push('extra')
    graph.nodes.extra = node({ task_id: 'extra', name: 'app.extra', first_seen: at(500) })
    await expect(flowNode(page, 'extra')).toHaveCount(1, { timeout: 15_000 })
    await expect(row(page, 'extra')).toHaveCount(1)

    expect(await scrollLeft(page)).toBe(rested)
  })
})

test.describe('narrow screens', () => {
  test('does not scroll the page sideways at 400px; the panel scrolls inside itself', async ({
    page,
  }) => {
    await openFlow(page, reallocationFlow(), 400)
    await rows(page).first().waitFor()

    const metrics = await page.evaluate(() => ({
      page: document.documentElement.scrollWidth - document.documentElement.clientWidth,
      box: (() => {
        const box = document.querySelector('[data-testid="gantt-box"]') as HTMLElement
        return box.scrollWidth - box.clientWidth
      })(),
    }))
    expect(metrics.page).toBeLessThanOrEqual(0)
    expect(metrics.box).toBeGreaterThan(0)
  })

  test('keeps labels at 12px at 400px', async ({ page }) => {
    await openFlow(page, reallocationFlow(), 400)
    await rows(page).first().waitFor()

    const sizes = await timelinePanel(page)
      .locator('text')
      .evaluateAll((els) => els.map((el) => Number.parseFloat(getComputedStyle(el).fontSize)))
    expect(Math.min(...sizes)).toBeGreaterThanOrEqual(12)
  })
})
