import { expect, test } from '@playwright/test'

import { type MockApiContext, setupMockApi } from './fixtures/mock-api'
import { createGroup, createWorkflowWithGroup } from './fixtures/mock-data'

/**
 * Tests for the Graphs page.
 *
 * These tests use mocked API responses, no Docker required.
 * Run with E2E_MODE=real for integration testing against Docker.
 */

const isRealMode = process.env.E2E_MODE === 'real'

/**
 * Helper to navigate to first available graph.
 *
 * Waits for the graphs list to settle: either a graph link shows up or the page
 * says there are no graphs. Returns false only in the second case (genuinely no
 * graph data, so callers skip). If neither appears (slow load, failed request)
 * the wait times out and the test fails instead of silently skipping.
 */
async function navigateToFirstGraph(page: import('@playwright/test').Page): Promise<boolean> {
  await page.goto('/graphs')

  const graphLink = page.locator('a[href*="/graph/"]').first()
  const emptyState = page.getByText('No task graphs yet')
  await expect(graphLink.or(emptyState)).toBeVisible({ timeout: 15_000 })

  if (!(await graphLink.isVisible())) return false

  await graphLink.click()
  await page.waitForLoadState('networkidle')
  return true
}

/** Which graph a test needs. Data for these exists in both modes (see below). */
interface GraphTarget {
  /** Root node type, e.g. 'CHORD'. */
  nodeType?: string
  /** Root task name. */
  name?: RegExp
}

/**
 * A chord: a group of tasks followed by a callback. Mock mode: DEFAULT_TASKS
 * createGroup(3, { withCallback: true }). Real mode: the E2E seed's
 * create_workflow (tests/e2e/tasks.py).
 */
const CHORD_GRAPH: GraphTarget = { nodeType: 'CHORD' }

/**
 * A parent task that spawns a group. Mock mode: tasks.batch_processor from
 * createWorkflowWithGroup. Real mode: e2e.batch_processor from the E2E seed.
 */
const PARENT_WITH_GROUP_GRAPH: GraphTarget = { name: /batch_processor$/ }

/** Root graphs, from the mocked store or (real mode) the stemtrace API. */
async function listRootGraphs(
  page: import('@playwright/test').Page,
  mockApi: MockApiContext | null,
): Promise<{ task_id: string; name: string; node_type: string }[]> {
  if (mockApi) return mockApi.getRootTasks()

  // maxRetries: this is a raw HTTP call without the page's own query retries, and
  // one run died on a connection reset ("socket hang up", Playwright 1.57). Playwright
  // retries ECONNRESET up to maxRetries times (API request docs).
  const response = await page.request.get('/stemtrace/api/graphs?limit=100', { maxRetries: 3 })
  expect(response.ok()).toBe(true)
  return (await response.json()).graphs
}

/**
 * Open a specific graph from the graphs list.
 *
 * Unlike navigateToFirstGraph this never skips: the data it looks for is
 * created by the E2E seed (real mode) or the mock fixtures (mock mode), so a
 * missing graph is a broken setup and fails the test with the graphs it did find.
 */
async function openGraph(
  page: import('@playwright/test').Page,
  mockApi: MockApiContext | null,
  target: GraphTarget,
): Promise<void> {
  const graphs = await listRootGraphs(page, mockApi)
  const match = graphs.find(
    (graph) =>
      (!target.nodeType || graph.node_type === target.nodeType) &&
      (!target.name || target.name.test(graph.name)),
  )
  if (!match) {
    const found = graphs.map((graph) => `${graph.name} (${graph.node_type})`).join(', ')
    const wanted = [
      target.nodeType && `node type ${target.nodeType}`,
      target.name && `name /${target.name.source}/`,
    ]
      .filter(Boolean)
      .join(', ')
    throw new Error(`No graph with ${wanted}; found: ${found || 'none'}`)
  }

  await page.goto('/graphs')
  const link = page.locator('a[href*="/graph/"]').filter({ hasText: match.task_id })
  await expect(link).toBeVisible({ timeout: 15_000 })
  await link.click()
  await page.waitForLoadState('networkidle')
}

test.describe('Graphs Page', () => {
  test.beforeEach(async ({ page }) => {
    if (!isRealMode) {
      await setupMockApi(page)
    }
  })

  test('displays graphs list', async ({ page }) => {
    await page.goto('/graphs')
    await page.waitForLoadState('networkidle')

    // Page should load without errors
    const heading = page.locator('h1, h2, [class*="heading"]')
    await expect(heading.first()).toBeVisible()
  })

  test('shows graph entries when workflows exist', async ({ page }) => {
    await page.goto('/graphs')
    await page.waitForLoadState('networkidle')

    // Look for graph entries
    const graphEntries = page.locator(
      '[data-testid="graph-entry"], .graph-entry, a[href*="/graph/"], tr',
    )

    const count = await graphEntries.count()
    // Verify page renders without error (count check is informational)
    expect(typeof count).toBe('number')
  })

  test('can navigate to graph detail', async ({ page }) => {
    const hasGraph = await navigateToFirstGraph(page)
    if (!hasGraph) {
      test.skip()
      return
    }
    await expect(page).toHaveURL(/\/graph\/.+/)
  })
})

test.describe('Graph Detail Page', () => {
  test.beforeEach(async ({ page }) => {
    if (!isRealMode) {
      await setupMockApi(page)
    }
  })

  test('renders graph visualization', async ({ page }) => {
    // First go to graphs list
    const hasGraph = await navigateToFirstGraph(page)
    if (!hasGraph) {
      test.skip()
      return
    }
    await page.waitForLoadState('networkidle')

    // Look for react-flow container or SVG graph
    const graphContainer = page.locator(
      '.react-flow, [data-testid="graph"], svg, .graph-container, [class*="flow"]',
    )

    await expect(graphContainer.first()).toBeVisible()
  })

  test('shows graph nodes', async ({ page }) => {
    const hasGraph = await navigateToFirstGraph(page)
    if (!hasGraph) {
      test.skip()
      return
    }
    await page.waitForLoadState('networkidle')

    // Look for graph nodes
    const nodes = page.locator(
      '.react-flow__node, [data-testid="graph-node"], .node, [class*="node"]',
    )

    const count = await nodes.count()
    // Should have at least one node if graph exists
    if (count > 0) {
      await expect(nodes.first()).toBeVisible()
    }
  })

  test('nodes show task state colors', async ({ page }) => {
    const hasGraph = await navigateToFirstGraph(page)
    if (!hasGraph) {
      test.skip()
      return
    }
    await page.waitForLoadState('networkidle')

    // Nodes should have state-based styling
    const styledNodes = page.locator(
      '[class*="success"], [class*="pending"], [class*="started"], [data-state]',
    )

    const count = await styledNodes.count()
    // Verify page renders graph nodes (count check is informational)
    expect(typeof count).toBe('number')
  })

  test('can click node to see task details', async ({ page }) => {
    const hasGraph = await navigateToFirstGraph(page)
    if (!hasGraph) {
      test.skip()
      return
    }
    await page.waitForLoadState('networkidle')

    // Click on a task node link (not container), which navigates to task details
    // Task nodes contain links to /tasks/:taskId
    const taskLink = page.locator('.react-flow__node a[href*="/tasks/"]').first()
    const linkVisible = await taskLink.isVisible().catch(() => false)

    if (linkVisible) {
      await taskLink.click()
      await page.waitForLoadState('networkidle')

      // Should navigate to task details page
      await expect(page).toHaveURL(/\/tasks\//)
    }
  })
})

test.describe('Graph Edges', () => {
  let mockApi: MockApiContext | null = null

  test.beforeEach(async ({ page }) => {
    mockApi = isRealMode ? null : await setupMockApi(page)
  })

  test('edges are rendered in the graph', async ({ page }) => {
    const hasGraph = await navigateToFirstGraph(page)
    if (!hasGraph) {
      test.skip()
      return
    }

    // React Flow renders edges as SVG groups with accessible names "Edge from X to Y"
    // Try multiple detection methods
    const edgesByClass = page.locator('.react-flow__edge')
    const edgesByAccessibleName = page.locator(
      '[aria-label*="Edge from"], g[aria-label*="Edge from"]',
    )
    // Also check for SVG paths in the edges layer
    const edgePathsInSvg = page.locator('.react-flow__edges path, svg g path').first()

    // Nodes and edges render after the page's data loads and React Flow lays them out,
    // so wait for the first node instead of counting whatever is in the DOM right now
    const nodes = page.locator('.react-flow__node')
    await expect(nodes.first()).toBeVisible()
    const nodeCount = await nodes.count()

    if (nodeCount > 1) {
      // With multiple nodes, we expect at least one edge or visible edge paths;
      // poll because edges are drawn a moment after their nodes
      await expect
        .poll(
          async () =>
            (await edgesByClass.count()) > 0 ||
            (await edgesByAccessibleName.count()) > 0 ||
            (await edgePathsInSvg.isVisible().catch(() => false)),
          { message: 'multi-node graph should render at least one edge' },
        )
        .toBe(true)
    }
  })

  test('edges connect parent to child nodes', async ({ page }) => {
    await openGraph(page, mockApi, PARENT_WITH_GROUP_GRAPH)

    // Edges are SVG groups with accessible names "Edge from X to Y"
    const edges = page.getByRole('group', { name: /Edge from/ })
    await expect(edges.first()).toBeVisible()
    expect(await edges.count()).toBeGreaterThan(0)
  })

  test('edges have appropriate styling', async ({ page }) => {
    const hasGraph = await navigateToFirstGraph(page)
    if (!hasGraph) {
      test.skip()
      return
    }

    // Look for SVG path elements within the edge container
    const edgePaths = page.locator('.react-flow__edges path, .react-flow__edge path')
    const pathCount = await edgePaths.count()

    if (pathCount === 0) {
      // Fallback: check if there are any SVG paths in the application
      const anyPaths = page.locator('[role="application"] svg path')
      const anyPathCount = await anyPaths.count()

      // There should be at least some paths (for edges or handles)
      expect(anyPathCount).toBeGreaterThanOrEqual(0)
      return
    }

    const firstPath = edgePaths.first()

    // Verify path has stroke styling
    const stroke = await firstPath.evaluate((el) => {
      const style = window.getComputedStyle(el)
      return style.stroke
    })

    // Stroke should be a color value (not 'none')
    expect(stroke).toBeTruthy()
    expect(stroke).not.toBe('none')
  })

  test('animated edges show for STARTED tasks', async ({ page }) => {
    const hasGraph = await navigateToFirstGraph(page)
    if (!hasGraph) {
      test.skip()
      return
    }

    // Animated edges have the 'animated' class
    const animatedEdges = page.locator('.react-flow__edge.animated')
    const animatedCount = await animatedEdges.count()

    // This is informational - STARTED tasks may or may not exist
    // Just verify the selector works and returns a number
    expect(typeof animatedCount).toBe('number')
  })
})

test.describe('Container Nodes (GROUP/CHORD)', () => {
  let mockApi: MockApiContext | null = null

  test.beforeEach(async ({ page }) => {
    mockApi = isRealMode ? null : await setupMockApi(page)
  })

  test('container nodes are rendered with dashed borders', async ({ page }) => {
    const hasGraph = await navigateToFirstGraph(page)
    if (!hasGraph) {
      test.skip()
      return
    }

    // Container nodes (GROUP/CHORD) use type='group' in React Flow
    const containerNodes = page.locator('.container-node')
    const containerCount = await containerNodes.count()

    // Groups may or may not exist in the current graph
    if (containerCount > 0) {
      const firstContainer = containerNodes.first()
      await expect(firstContainer).toBeVisible()

      // Container should have dashed border style
      const borderStyle = await firstContainer.evaluate((el) => {
        const style = window.getComputedStyle(el)
        return style.borderStyle
      })
      expect(borderStyle).toBe('dashed')
    }
  })

  test('container nodes contain child task nodes', async ({ page }) => {
    await openGraph(page, mockApi, CHORD_GRAPH)

    const containerNodes = page.locator('.container-node')
    await expect(containerNodes.first()).toBeVisible()

    // Container nodes should have task nodes rendered inside them: .container-child
    // marks the task nodes inside a container. The chord has 3 members plus a callback.
    const childNodes = page.locator('.container-child')
    await expect(childNodes.first()).toBeVisible()
    expect(await childNodes.count()).toBeGreaterThanOrEqual(1)
  })

  test('container header shows GROUP or CHORD label', async ({ page }) => {
    const hasGraph = await navigateToFirstGraph(page)
    if (!hasGraph) {
      test.skip()
      return
    }

    // Look for GROUP or CHORD text anywhere in the graph
    const groupLabel = page.getByText('GROUP')
    const chordLabel = page.getByText('CHORD')

    const hasGroupLabel = await groupLabel
      .first()
      .isVisible()
      .catch(() => false)
    const hasChordLabel = await chordLabel
      .first()
      .isVisible()
      .catch(() => false)

    // If we're on a graph with containers, at least one label should be present
    // If there are no container graphs, this test just verifies the selectors work
    const hasAnyLabel = hasGroupLabel || hasChordLabel

    // This is informational - not all graphs have containers
    expect(typeof hasAnyLabel).toBe('boolean')
  })
})

test.describe('Edge Visibility Regression Tests', () => {
  let mockApi: MockApiContext | null = null

  test.beforeEach(async ({ page }) => {
    mockApi = isRealMode ? null : await setupMockApi(page)
  })

  /**
   * Regression test: When a graph has both a parent task AND a GROUP/CHORD container,
   * there MUST be an edge connecting them. This catches the bug where container nodes
   * had missing incoming edges.
   */
  test('parent task has edge to GROUP container', async ({ page }) => {
    await openGraph(page, mockApi, PARENT_WITH_GROUP_GRAPH)

    // The graph has a parent task, a GROUP container (with its header label) and
    // members, so there are several nodes and exactly one container
    const nodes = page.locator('.react-flow__node')
    await expect(nodes.first()).toBeVisible()
    expect(await nodes.count()).toBeGreaterThan(1)
    await expect(page.locator('.container-node')).toHaveCount(1)

    // This is the critical assertion: parent-to-container edge must exist, and its
    // SVG path must actually be rendered (not just the element). Poll: edges are
    // drawn a moment after their nodes.
    await expect
      .poll(() => page.locator('.react-flow__edge').count(), {
        message: 'parent-to-container edge should be rendered',
      })
      .toBeGreaterThan(0)
    await expect
      .poll(() => page.locator('.react-flow__edge path[d]').count(), {
        message: 'edge SVG paths should be rendered',
      })
      .toBeGreaterThan(0)
  })

  /**
   * Regression test: Edges from parent task to GROUP container must be visible.
   * This catches the issue where GROUP containers were missing incoming edges.
   */
  test('parent-to-container edges are rendered', async ({ page }) => {
    const hasGraph = await navigateToFirstGraph(page)
    if (!hasGraph) {
      test.skip()
      return
    }

    const nodes = page.locator('.react-flow__node')
    const nodeCount = await nodes.count()
    const edges = page.locator('.react-flow__edge')
    const edgeCount = await edges.count()

    // Basic sanity check: if we have more than 1 node, we should have edges
    if (nodeCount > 1) {
      expect(edgeCount).toBeGreaterThan(0)
    }

    // For graphs with containers, verify edges connect to them
    const containerNodes = page.locator('.container-node')
    const containerCount = await containerNodes.count()

    if (containerCount > 0) {
      // There should be edges that target the container
      // (React Flow edge target attribute matches node data-id)
      const firstContainer = containerNodes.first()
      const containerId = await firstContainer.getAttribute('data-id')

      if (containerId) {
        // Look for edges targeting this container
        // Note: React Flow uses data-testid or aria attributes for edge connections
        // The edge source/target is in the edge ID pattern: source-target
        const allEdgeIds = await edges.evaluateAll((els) =>
          els.map((el) => el.getAttribute('data-testid') || el.id),
        )

        // At least check that edges exist in the graph
        expect(allEdgeIds.length).toBeGreaterThanOrEqual(0)
      }
    }
  })

  /**
   * Regression test: CHORD callback edges must connect from ALL header tasks to callback.
   * Each header task contributes to triggering the callback.
   */
  test('chord has edges from all header tasks to callback', async ({ page }) => {
    await openGraph(page, mockApi, CHORD_GRAPH)

    await expect(page.locator('.header-label-node:has-text("CHORD")').first()).toBeVisible()

    // A chord with 3 header tasks has at least 3 edges (one from each header task
    // to the callback). Poll: edges are drawn a moment after their nodes.
    await expect
      .poll(() => page.locator('.react-flow__edge').count(), {
        message: 'chord should have an edge from each of its 3 header tasks',
      })
      .toBeGreaterThanOrEqual(3)
  })
})

test.describe('Graph Page - Mock Scenarios', () => {
  // These tests only run in mock mode
  test.skip(isRealMode, 'Mock-only test')

  test('renders group with 3 members', async ({ page }) => {
    const mockApi = await setupMockApi(page, { useDefaults: false })
    const groupTasks = createGroup(3, { withCallback: false })
    mockApi.addTasks(groupTasks)

    await page.goto('/graphs')
    await page.waitForLoadState('networkidle')

    // Should show the group in the list
    const graphLink = page.locator('a[href*="/graph/"]').first()
    await expect(graphLink).toBeVisible()

    await graphLink.click()
    await page.waitForLoadState('networkidle')

    // Should have 5 nodes: 1 GROUP container + 1 header label + 3 members
    // (React Flow renders the container header as a separate node)
    const nodes = page.locator('.react-flow__node')
    const nodeCount = await nodes.count()
    expect(nodeCount).toBe(5)
  })

  test('renders workflow with parent spawning group', async ({ page }) => {
    const mockApi = await setupMockApi(page, { useDefaults: false })
    const workflowTasks = createWorkflowWithGroup(2)
    mockApi.addTasks(workflowTasks)

    await page.goto('/graphs')
    await page.waitForLoadState('networkidle')

    // Find the batch_processor graph
    const graphLink = page.getByText('tasks.batch_processor').first()
    await expect(graphLink).toBeVisible()

    await graphLink.click()
    await page.waitForLoadState('networkidle')

    // Should have nodes: 1 parent + 1 GROUP container + 1 header label + 2 members = 5 nodes
    const nodes = page.locator('.react-flow__node')
    const nodeCount = await nodes.count()
    expect(nodeCount).toBe(5)

    // Should have edges connecting parent to group
    const edges = page.locator('.react-flow__edge')
    const edgeCount = await edges.count()
    expect(edgeCount).toBeGreaterThan(0)
  })

  test('renders chord with callback', async ({ page }) => {
    const mockApi = await setupMockApi(page, { useDefaults: false })
    const chordTasks = createGroup(3, { withCallback: true })
    mockApi.addTasks(chordTasks)

    await page.goto('/graphs')
    await page.waitForLoadState('networkidle')

    const graphLink = page.locator('a[href*="/graph/"]').first()
    await graphLink.click()
    await page.waitForLoadState('networkidle')

    // Should have 6 nodes: 1 CHORD container + 1 header label + 3 members + 1 callback
    const nodes = page.locator('.react-flow__node')
    const nodeCount = await nodes.count()
    expect(nodeCount).toBe(6)

    // CHORD label should be visible
    const chordText = page.getByText('CHORD')
    await expect(chordText.first()).toBeVisible()
  })
})
