import { expect, type Page, test } from '@playwright/test'

import {
  flowContainerMembers,
  flowContainers,
  flowEdges,
  flowNode,
  flowNodes,
} from './fixtures/flow'
import {
  allCaptures,
  capture,
  chain,
  type FlowGraph,
  group,
  node,
  serveFlow,
  type TaskDetail,
} from './fixtures/flow-graphs'
import { setupMockApi } from './fixtures/mock-api'

/**
 * The Flow page's behaviour: selection, Jump to failure, zoom, the inspector, collapsing and the
 * not-run box. The graphs are served as the API sends them (captures, or small synthetic ones),
 * so the page is tested against real shapes. Mock mode only.
 */

test.skip(process.env.E2E_MODE === 'real', 'Mock-only test')

async function openFlow(
  page: Page,
  graph: FlowGraph,
  details: Record<string, TaskDetail> = {},
): Promise<void> {
  await setupMockApi(page, { useDefaults: false })
  await serveFlow(page, graph, details)
  await page.goto(`/flows/${encodeURIComponent(graph.root_id)}`)
  await flowNodes(page).first().waitFor({ state: 'attached' })
}

const canvas = (page: Page) => page.getByTestId('flow-canvas')
const scrollLeft = (page: Page) => canvas(page).evaluate((el) => el.scrollLeft)
const inspector = (page: Page) => page.getByRole('complementary', { name: 'Inspector' })

/** c0 -> ... -> c<length-1>: too wide for the box; the listed steps failed. */
function longChain(length: number, failed: number[] = []): FlowGraph {
  const nodes = chain(length)
  for (const [i, entry] of Object.values(nodes).entries()) {
    entry.name = `app.step_${i}`
    entry.first_seen = new Date(2026, 0, 1, 0, 0, i).toISOString()
    entry.runtime_ms = 1200
  }
  for (const index of failed) {
    nodes[`c${index}`].state = 'FAILURE'
    nodes[`c${index}`].exception_type = 'ValueError'
  }
  return { root_id: 'c0', nodes }
}

/** A chord of 48 whose header 40 failed, so its callback did not run. */
function failedChord(): FlowGraph {
  const nodes = group(48, { 40: 'FAILURE' }, 'CHORD')
  nodes.g.not_run_reason = 'header_failed'
  nodes.g.state = 'FAILURE'
  return { root_id: 'g', nodes }
}

test.describe('selection', () => {
  test('chooses the first failure on open and leaves the view at the root', async ({ page }) => {
    await openFlow(page, longChain(10, [9]))

    await expect(flowNode(page, 'c9')).toHaveAttribute('aria-pressed', 'true')
    await expect(inspector(page)).toContainText('step_9')
    await expect(inspector(page)).toContainText('Failure')
    expect(await scrollLeft(page)).toBe(0)
    await expect(flowNode(page, 'c0')).toBeInViewport()
    await expect(flowNode(page, 'c9')).not.toBeInViewport()
  })

  test('chooses the root when nothing failed', async ({ page }) => {
    await openFlow(page, longChain(3))

    await expect(flowNode(page, 'c0')).toHaveAttribute('aria-pressed', 'true')
  })

  test('chooses a node with a click and shows it in the inspector', async ({ page }) => {
    await openFlow(page, longChain(3, [2]))

    await flowNode(page, 'c1').click()

    await expect(flowNode(page, 'c1')).toHaveAttribute('aria-pressed', 'true')
    await expect(flowNode(page, 'c2')).toHaveAttribute('aria-pressed', 'false')
    await expect(inspector(page)).toContainText('step_1')
  })

  test('walks the nodes with Tab in layout order and chooses with Enter and Space', async ({
    page,
  }) => {
    const nodes = group(3, {}, 'CHORD')
    nodes.g.chord_id = 'cb'
    nodes.g.children.push('cb')
    nodes.cb = node({ task_id: 'cb', name: 'app.total' })
    await openFlow(page, { root_id: 'g', nodes })
    const focused = () =>
      page.evaluate(() => document.activeElement?.getAttribute('data-task-id') ?? null)

    await page.locator('[data-task-id="g"]').focus()
    const order = [await focused()]
    for (let i = 0; i < 4; i++) {
      await page.keyboard.press('Tab')
      order.push(await focused())
    }
    expect(order).toEqual(['g', 'm0', 'm1', 'm2', 'cb'])

    await page.locator('[data-task-id="m1"]').focus()
    await page.keyboard.press('Enter')
    await expect(flowNode(page, 'm1')).toHaveAttribute('aria-pressed', 'true')
    await page.locator('[data-task-id="cb"]').focus()
    await page.keyboard.press(' ')
    await expect(flowNode(page, 'cb')).toHaveAttribute('aria-pressed', 'true')
    await expect(flowNode(page, 'm1')).toHaveAttribute('aria-pressed', 'false')
    await expect(inspector(page)).toContainText('total')
  })

  test('tells every state by icon and word in the accessible name', async ({ page }) => {
    const graph = longChain(4, [1])
    graph.nodes.c2.state = 'STARTED'
    graph.nodes.c3.state = 'RETRY'
    await openFlow(page, graph)

    await expect(flowNode(page, 'c0')).toHaveAttribute('aria-label', 'app.step_0, Success, 1.2s')
    await expect(flowNode(page, 'c1')).toHaveAttribute(
      'aria-label',
      'app.step_1, Failure, ValueError, 1.2s',
    )
    await expect(flowNode(page, 'c2')).toHaveAttribute('aria-label', 'app.step_2, Running, 1.2s')
    await expect(flowNode(page, 'c3')).toHaveAttribute('aria-label', 'app.step_3, Retry, 1.2s')
    // each block draws its state's icon beside the word
    for (const id of ['c0', 'c1', 'c2', 'c3']) {
      await expect(flowNode(page, id).locator('svg')).toHaveCount(1)
    }
  })
})

test.describe('Jump to failure', () => {
  test('is disabled with a reason when nothing failed', async ({ page }) => {
    await openFlow(page, longChain(3))

    const jump = page.getByRole('button', { name: 'Jump to failure' })
    await expect(jump).toBeDisabled()
    await expect(jump).toHaveAttribute('title', 'No failed task in this flow')
  })

  test('chooses the first failure by start time and centres it', async ({ page }) => {
    // c7 failed before c9 did, though it comes first in no list: start time decides
    const graph = longChain(10, [7, 9])
    graph.nodes.c9.first_seen = new Date(2026, 0, 1, 0, 0, 30).toISOString()
    await openFlow(page, graph)
    await flowNode(page, 'c0').click()

    const jump = page.getByRole('button', { name: 'Jump to failure' })
    await expect(jump).toBeEnabled()
    await jump.click()

    await expect(flowNode(page, 'c7')).toHaveAttribute('aria-pressed', 'true')
    await expect(inspector(page)).toContainText('step_7')
    await expect.poll(() => scrollLeft(page)).toBeGreaterThan(0)
    await expect(flowNode(page, 'c7')).toBeInViewport({ ratio: 0.99 })
    // centred: its middle is within a block's width of the box's middle
    const box = (await canvas(page).boundingBox())!
    const at = (await flowNode(page, 'c7').boundingBox())!
    expect(Math.abs(at.x + at.width / 2 - (box.x + box.width / 2))).toBeLessThan(100)
  })

  test('counts a rejected task as a failure', async ({ page }) => {
    const graph = longChain(4)
    graph.nodes.c2.state = 'REJECTED'
    await openFlow(page, graph)

    await expect(page.getByRole('button', { name: 'Jump to failure' })).toBeEnabled()
    await expect(flowNode(page, 'c2')).toHaveAttribute('aria-pressed', 'true')
  })
})

test.describe('zoom', () => {
  const zoomOut = (page: Page) => page.getByRole('button', { name: 'Zoom out' })
  const zoomIn = (page: Page) => page.getByRole('button', { name: 'Zoom in' })
  const fit = (page: Page) => page.getByRole('group', { name: 'Zoom' }).getByRole('button').nth(1)

  test('steps from Fit to 140% and back, disabling each end', async ({ page }) => {
    await openFlow(page, longChain(3))

    await expect(fit(page)).toHaveText('Fit')
    await expect(zoomOut(page)).toBeDisabled()
    await expect(zoomIn(page)).toBeEnabled()

    await zoomIn(page).click()
    await expect(fit(page)).toHaveText('120%')
    await expect(zoomOut(page)).toBeEnabled()
    await expect(zoomIn(page)).toBeEnabled()

    await zoomIn(page).click()
    await expect(fit(page)).toHaveText('140%')
    await expect(zoomIn(page)).toBeDisabled()
    await expect(zoomOut(page)).toBeEnabled()

    await zoomOut(page).click()
    await zoomOut(page).click()
    await expect(fit(page)).toHaveText('100%')
    await expect(zoomOut(page)).toBeDisabled()

    await fit(page).click()
    await expect(fit(page)).toHaveText('Fit')
  })

  test('draws at the natural width at Fit and scales the drawing with the zoom', async ({
    page,
  }) => {
    await openFlow(page, longChain(3))
    const width = () =>
      page
        .locator('[data-testid="flow-canvas"] > svg')
        .evaluate((el) => el.getBoundingClientRect().width)

    expect(await width()).toBeCloseTo(668, 0)
    await zoomIn(page).click()
    expect(await width()).toBeCloseTo(668 * 1.2, 0)
    await zoomIn(page).click()
    expect(await width()).toBeCloseTo(668 * 1.4, 0)
  })

  test('keeps text at 12px or more at every zoom', async ({ page }) => {
    await openFlow(page, longChain(3))
    const smallest = () =>
      page.evaluate(() => {
        const sizes = [...document.querySelectorAll('[data-testid="flow-canvas"] text')].map(
          (el) => {
            const box = el.getBoundingClientRect()
            const user = Number.parseFloat(getComputedStyle(el).fontSize)
            const svg = el.ownerSVGElement as SVGSVGElement
            const scale = svg.getBoundingClientRect().width / svg.viewBox.baseVal.width
            return user * scale || box.height
          },
        )
        return Math.min(...sizes)
      })

    expect(await smallest()).toBeGreaterThanOrEqual(12)
    await zoomIn(page).click()
    expect(await smallest()).toBeGreaterThanOrEqual(12)
  })
})

test.describe('inspector', () => {
  test('shows the rows a task has, its kwargs (at most four) and a link to its page', async ({
    page,
  }) => {
    const graph = longChain(2)
    Object.assign(graph.nodes.c1, {
      hostname: 'worker@host-1',
      queue: 'emails',
      queue_wait_ms: 250,
      runtime_ms: 1500,
    })
    await openFlow(page, graph, {
      c1: { kwargs: { a: 1, b: 'two', c: [3], d: null, e: 'fifth' } },
    })
    await flowNode(page, 'c1').click()

    const panel = inspector(page)
    await expect(panel.getByText('Worker', { exact: true })).toBeVisible()
    await expect(panel.getByRole('link', { name: 'worker@host-1' })).toHaveAttribute(
      'href',
      /\/executions\?.*hostname=worker%40host-1/,
    )
    await expect(panel.getByText('emails')).toBeVisible()
    await expect(panel.getByText('250ms')).toBeVisible()
    await expect(panel.getByText('1.5s')).toBeVisible()
    await expect(panel.getByText('"two"')).toBeVisible()
    await expect(panel.getByText('"fifth"')).toHaveCount(0)
    await expect(panel.getByRole('link', { name: 'Open full detail' })).toHaveAttribute(
      'href',
      /\/tasks\/c1$/,
    )
    await expect(panel.getByRole('button', { name: 'Copy task ID' })).toBeVisible()
  })

  test('leaves out a row with no value, never a dash', async ({ page }) => {
    const graph = longChain(2)
    delete graph.nodes.c1.runtime_ms
    await openFlow(page, graph)
    await flowNode(page, 'c1').click()

    const panel = inspector(page)
    for (const label of ['Worker', 'Queue', 'Wait', 'Runtime', 'Exception', 'Kwargs']) {
      await expect(panel.getByText(label, { exact: true })).toHaveCount(0)
    }
    await expect(panel).not.toContainText('–')
    await expect(panel).not.toContainText('—')
    await expect(panel.getByRole('link', { name: 'Open full detail' })).toBeVisible()
  })

  test('shows the exception message of a failure', async ({ page }) => {
    await openFlow(page, longChain(2, [1]), { c1: { exception: 'ValueError: no stock' } })

    await expect(inspector(page).getByText('ValueError: no stock')).toBeVisible()
  })

  test('shows a container with its members, callback reason and an expand button', async ({
    page,
  }) => {
    await openFlow(page, failedChord())
    // The header: a member sits over the middle of the box
    await flowNode(page, 'g').click({ position: { x: 10, y: 10 } })

    const panel = inspector(page)
    await expect(panel).toContainText('Chord')
    await expect(panel.getByText('48 tasks: 47 succeeded, 1 failed')).toBeVisible()
    await expect(
      panel.getByText('Not run: Chord header failed: 1 of 48 tasks failed'),
    ).toBeVisible()
    await panel.getByRole('button', { name: 'Expand all 48 members' }).click()
    await expect(flowContainerMembers(page)).toHaveCount(48)
    await expect(panel.getByRole('button', { name: 'Collapse members' })).toBeVisible()
  })

  test('shows a container with no collapse button when it has few members', async ({ page }) => {
    await openFlow(page, { root_id: 'g', nodes: group(3) })
    // The header: a member sits over the middle of the box
    await flowNode(page, 'g').click({ position: { x: 10, y: 10 } })

    await expect(inspector(page).getByRole('button', { name: /members/ })).toHaveCount(0)
  })
})

test.describe('many members', () => {
  test('shows the first three and the failed header of 48, and expands and collapses', async ({
    page,
  }) => {
    await openFlow(page, failedChord())

    await expect(flowContainerMembers(page)).toHaveCount(4)
    await expect(flowNode(page, 'm40')).toBeVisible()
    await expect(flowNode(page, 'm3')).toHaveCount(0)
    const more = page.getByTestId('flow-more')
    await expect(more).toContainText('+44 more')
    await expect(more).toContainText('all succeeded')
    await expect(flowContainers(page)).toHaveCount(1)

    await more.click()
    await expect(flowContainerMembers(page)).toHaveCount(48)
    await expect(page.getByTestId('flow-more')).toContainText('Collapse')
    // four to a row: 12 tracks and the collapse block
    await expect(page.locator('[data-testid="flow-more"][data-kind="less"]')).toHaveCount(1)

    await page.getByTestId('flow-more').click()
    await expect(flowContainerMembers(page)).toHaveCount(4)
  })

  test('opens a block from the keyboard', async ({ page }) => {
    await openFlow(page, failedChord())

    await page.getByTestId('flow-more').focus()
    await page.keyboard.press('Enter')

    await expect(flowContainerMembers(page)).toHaveCount(48)
  })
})

test.describe('callback not run', () => {
  const cases = [
    ['chord_failed_header@field', 'Chord header failed: 1 of 2 tasks failed'],
    ['chord_rejected_header', 'Chord header failed: 1 of 2 tasks failed'],
    ['chord_ignored_header', 'Chord header ignored'],
  ] as const

  for (const [name, text] of cases) {
    test(`draws a dashed box saying "${text}" (${name})`, async ({ page }) => {
      const graph = capture(name)
      await openFlow(page, graph)

      const box = page.getByTestId('flow-not-run')
      await expect(box).toHaveCount(1)
      await expect(box).toContainText('not run')
      await expect(box).toContainText(text.split(': ')[0])
      await expect(box).toHaveAttribute('aria-label', `Callback not run, ${text}`)
      await expect(box.locator('svg')).toHaveCount(1)
      const edge = page.locator(
        `[data-testid="flow-edge"][data-to="${graph.root_id}:not-run"] path`,
      )
      await expect(edge.first()).toHaveClass(/flow-track-nr/)
    })
  }

  test('says a revoked header in words', async ({ page }) => {
    const graph = capture('chord_failed_header@field')
    graph.nodes[graph.root_id].not_run_reason = 'header_revoked'
    await openFlow(page, graph)

    await expect(page.getByTestId('flow-not-run')).toHaveAttribute(
      'aria-label',
      'Callback not run, Chord header revoked',
    )
  })

  test('chooses the chord when the box is chosen', async ({ page }) => {
    const graph = capture('chord_failed_header@field')
    await openFlow(page, graph)

    await page.getByTestId('flow-not-run').click()

    await expect(flowNode(page, graph.root_id)).toHaveAttribute('aria-pressed', 'true')
    await expect(inspector(page)).toContainText('Not run: Chord header failed')
  })

  test('draws no box once the callback ran', async ({ page }) => {
    await openFlow(page, capture('chord@field'))

    await expect(page.getByTestId('flow-not-run')).toHaveCount(0)
    await expect(flowEdges(page)).toHaveCount(3)
  })
})

test.describe('every capture', () => {
  // Opening each real shape in the page, not just the layout: nothing throws, every node is drawn
  for (const name of [
    'chord@field',
    'chord_with_chain_body@field',
    'group_of_chains',
    'chord_in_chord_body@field',
    'replace_group',
  ]) {
    test(`draws ${name} with every node once`, async ({ page }) => {
      const graph = capture(name)
      const errors: string[] = []
      page.on('pageerror', (error) => errors.push(error.message))
      await openFlow(page, graph)

      const ids = await flowNodes(page).evaluateAll((els) =>
        els.map((el) => el.getAttribute('data-task-id')),
      )
      expect(new Set(ids).size).toBe(ids.length)
      expect(ids.length).toBe(Object.keys(graph.nodes).length)
      expect(errors).toEqual([])
    })
  }

  test('has captures for every shape the spec names', () => {
    const names = Object.keys(allCaptures())
    for (const wanted of [
      'chain',
      'group',
      'chord@field',
      'chord_with_chain_body@field',
      'group_of_chains',
    ]) {
      expect(names).toContain(wanted)
    }
  })
})

test.describe('live updates keep the view', () => {
  test('does not move the view once a node has been chosen', async ({ page }) => {
    const graph = longChain(10)
    await setupMockApi(page, { useDefaults: false })
    await serveFlow(page, graph)
    // The socket closes at once, so the page polls the graph every 5 s
    await page.routeWebSocket(/\/ws$/, (ws) => ws.close())
    await page.goto('/flows/c0')
    await flowNodes(page).first().waitFor({ state: 'attached' })
    await flowNode(page, 'c1').click()

    graph.nodes.c9.children = ['c10']
    graph.nodes.c10 = node({ task_id: 'c10', name: 'app.step_10' })
    await expect(flowNode(page, 'c10')).toHaveCount(1, { timeout: 15_000 })

    expect(await scrollLeft(page)).toBe(0)
  })

  // Whatever the reader scrolls with, the view stays: the scrollbar thumb (a plain scrollLeft
  // change), the wheel, the arrow keys, and Tab to a node that is out of the box
  const scrolls: Record<string, (page: Page) => Promise<void>> = {
    'the scrollbar': (page) =>
      canvas(page).evaluate((el) => {
        el.scrollLeft = el.scrollWidth
      }),
    'the wheel': async (page) => {
      const box = (await canvas(page).boundingBox())!
      await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2)
      await page.mouse.wheel(900, 0)
    },
    'the arrow keys': async (page) => {
      await canvas(page).focus()
      for (let i = 0; i < 30; i++) await page.keyboard.press('ArrowRight')
    },
    'Tab to a node out of the box': (page) => flowNode(page, 'c11').focus(),
  }
  for (const [input, scroll] of Object.entries(scrolls)) {
    test(`does not move the view after a scroll with ${input}`, async ({ page }) => {
      const graph = longChain(12)
      await setupMockApi(page, { useDefaults: false })
      await serveFlow(page, graph)
      await page.routeWebSocket(/\/ws$/, (ws) => ws.close())
      await page.goto('/flows/c0')
      await flowNodes(page).first().waitFor({ state: 'attached' })

      await scroll(page)
      await expect.poll(() => scrollLeft(page)).toBeGreaterThan(300)
      // Keys and the wheel scroll in steps: wait until the box has come to rest
      let scrolled = -1
      await expect
        .poll(
          async () => {
            const now = await scrollLeft(page)
            const rested = now === scrolled
            scrolled = now
            return rested
          },
          { intervals: [250] },
        )
        .toBe(true)

      // A new child of the first task, far left of where the reader is looking
      graph.nodes.c0.children.push('extra')
      graph.nodes.extra = node({ task_id: 'extra', name: 'app.extra' })
      await expect(flowNode(page, 'extra')).toHaveCount(1, { timeout: 15_000 })

      expect(await scrollLeft(page)).toBe(scrolled)
    })
  }

  test('scrolls a new node into the box while nobody has moved the view', async ({ page }) => {
    const graph = longChain(3)
    await setupMockApi(page, { useDefaults: false })
    await serveFlow(page, graph)
    await page.routeWebSocket(/\/ws$/, (ws) => ws.close())
    await page.goto('/flows/c0')
    await flowNodes(page).first().waitFor({ state: 'attached' })

    for (const [prev, id] of [
      ['c2', 'c3'],
      ['c3', 'c4'],
      ['c4', 'c5'],
    ]) {
      graph.nodes[prev].children = [id]
      graph.nodes[id] = node({ task_id: id, name: `app.step_${id.slice(1)}` })
    }
    await expect(flowNode(page, 'c5')).toHaveCount(1, { timeout: 15_000 })

    await expect(flowNode(page, 'c5')).toBeInViewport({ ratio: 0.99 })
    expect(await scrollLeft(page)).toBeGreaterThan(0)

    // The scroll the page made itself is not the reader's: the next new node is revealed too
    for (const [prev, id] of [
      ['c5', 'c6'],
      ['c6', 'c7'],
    ]) {
      graph.nodes[prev].children = [id]
      graph.nodes[id] = node({ task_id: id, name: `app.step_${id.slice(1)}` })
    }
    await expect(flowNode(page, 'c7')).toHaveCount(1, { timeout: 15_000 })
    await expect(flowNode(page, 'c7')).toBeInViewport({ ratio: 0.99 })
  })
})
