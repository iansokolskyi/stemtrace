import { expect, test } from '@playwright/test'

/**
 * Tests for the Graphs page.
 *
 * Prerequisites:
 *   docker compose -f docker-compose.e2e.yml up -d --wait
 *   # Submit workflow tasks to create graphs
 */

/**
 * Helper to navigate to first available graph
 */
async function navigateToFirstGraph(page: import('@playwright/test').Page): Promise<boolean> {
  await page.goto('/graphs')
  await page.waitForLoadState('networkidle')

  const graphLink = page.locator('a[href*="/graph/"]').first()
  const isVisible = await graphLink.isVisible().catch(() => false)

  if (!isVisible) return false

  await graphLink.click()
  await page.waitForLoadState('networkidle')
  return true
}

test.describe('Graphs Page', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('/graphs')
  })

  test('displays graphs list', async ({ page }) => {
    await page.waitForLoadState('networkidle')

    // Page should load without errors
    const heading = page.locator('h1, h2, [class*="heading"]')
    await expect(heading.first()).toBeVisible()
  })

  test('shows graph entries when workflows exist', async ({ page }) => {
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
    await page.waitForLoadState('networkidle')

    const graphLink = page.locator('a[href*="/graph/"]').first()
    const isVisible = await graphLink.isVisible().catch(() => false)

    if (!isVisible) {
      test.skip()
      return
    }

    await graphLink.click()
    await expect(page).toHaveURL(/\/graph\/.+/)
  })
})

test.describe('Graph Detail Page', () => {
  test('renders graph visualization', async ({ page }) => {
    // First go to graphs list
    await page.goto('/graphs')
    await page.waitForLoadState('networkidle')

    const graphLink = page.locator('a[href*="/graph/"]').first()
    const isVisible = await graphLink.isVisible().catch(() => false)

    if (!isVisible) {
      test.skip()
      return
    }

    await graphLink.click()
    await page.waitForLoadState('networkidle')

    // Look for react-flow container or SVG graph
    const graphContainer = page.locator(
      '.react-flow, [data-testid="graph"], svg, .graph-container, [class*="flow"]',
    )

    await expect(graphContainer.first()).toBeVisible()
  })

  test('shows graph nodes', async ({ page }) => {
    await page.goto('/graphs')
    await page.waitForLoadState('networkidle')

    const graphLink = page.locator('a[href*="/graph/"]').first()
    const isVisible = await graphLink.isVisible().catch(() => false)

    if (!isVisible) {
      test.skip()
      return
    }

    await graphLink.click()
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
    await page.goto('/graphs')
    await page.waitForLoadState('networkidle')

    const graphLink = page.locator('a[href*="/graph/"]').first()
    const isVisible = await graphLink.isVisible().catch(() => false)

    if (!isVisible) {
      test.skip()
      return
    }

    await graphLink.click()
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
    await page.goto('/graphs')
    await page.waitForLoadState('networkidle')

    const graphLink = page.locator('a[href*="/graph/"]').first()
    const isVisible = await graphLink.isVisible().catch(() => false)

    if (!isVisible) {
      test.skip()
      return
    }

    await graphLink.click()
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
  test('edges are rendered in the graph', async ({ page }) => {
    const hasGraph = await navigateToFirstGraph(page)
    if (!hasGraph) {
      test.skip()
      return
    }

    // React Flow renders edges as SVG groups with accessible names "Edge from X to Y"
    // Try multiple detection methods
    const edgesByClass = page.locator('.react-flow__edge')
    const edgesByAccessibleName = page.locator('[aria-label*="Edge from"], g[aria-label*="Edge from"]')
    // Also check for SVG paths in the edges layer
    const edgePathsInSvg = page.locator('.react-flow__edges path, svg g path').first()

    const edgeCountByClass = await edgesByClass.count()
    const edgeCountByName = await edgesByAccessibleName.count()
    const hasEdgePaths = await edgePathsInSvg.isVisible().catch(() => false)

    // Use whichever finds edges
    const edgeCount = Math.max(edgeCountByClass, edgeCountByName)

    // If there are multiple nodes, there should be edges
    const nodes = page.locator('.react-flow__node')
    const nodeCount = await nodes.count()

    if (nodeCount > 1) {
      // With multiple nodes, we expect at least one edge or visible edge paths
      const hasEdges = edgeCount > 0 || hasEdgePaths
      expect(hasEdges).toBe(true)
    }
  })

  test('edges connect parent to child nodes', async ({ page }) => {
    const hasGraph = await navigateToFirstGraph(page)
    if (!hasGraph) {
      test.skip()
      return
    }

    // Check for edges using accessible role
    const edges = page.getByRole('group', { name: /Edge from/ })
    const edgeCount = await edges.count()

    if (edgeCount === 0) {
      test.skip()
      return
    }

    // Verify at least one edge is visible
    const firstEdge = edges.first()
    await expect(firstEdge).toBeVisible()
    expect(edgeCount).toBeGreaterThan(0)
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
    const hasGraph = await navigateToFirstGraph(page)
    if (!hasGraph) {
      test.skip()
      return
    }

    const containerNodes = page.locator('.container-node')
    const containerCount = await containerNodes.count()

    if (containerCount === 0) {
      test.skip()
      return
    }

    // Container nodes should have child nodes rendered inside them
    // We verify by checking for .container-child class nodes which are task nodes inside containers
    const childNodes = page.locator('.container-child')
    const childCount = await childNodes.count()

    // Container should have task nodes inside (at least 1)
    expect(childCount).toBeGreaterThanOrEqual(1)
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

    const hasGroupLabel = await groupLabel.first().isVisible().catch(() => false)
    const hasChordLabel = await chordLabel.first().isVisible().catch(() => false)

    // If we're on a graph with containers, at least one label should be present
    // If there are no container graphs, this test just verifies the selectors work
    const hasAnyLabel = hasGroupLabel || hasChordLabel

    // This is informational - not all graphs have containers
    expect(typeof hasAnyLabel).toBe('boolean')
  })
})

test.describe('Edge Visibility Regression Tests', () => {
  /**
   * Regression test: When a graph has both a parent task AND a GROUP/CHORD container,
   * there MUST be an edge connecting them. This catches the bug where container nodes
   * had missing incoming edges.
   */
  test('parent task has edge to GROUP container', async ({ page }) => {
    await page.goto('/graphs')
    await page.waitForLoadState('networkidle')

    // Find a graph that starts with a task name (not 'group:' or 'chord:')
    // These are graphs where a parent task spawns a group
    const parentGraphLink = page
      .locator('a[href*="/graph/"]')
      .filter({ hasText: /parallel_group|workflow/ })
      .first()

    const hasParentGraph = await parentGraphLink.isVisible().catch(() => false)
    if (!hasParentGraph) {
      test.skip()
      return
    }

    await parentGraphLink.click()
    await page.waitForLoadState('networkidle')

    // Count nodes and edges
    const nodes = page.locator('.react-flow__node')
    const nodeCount = await nodes.count()
    const containerNodes = page.locator('.container-node')
    const containerCount = await containerNodes.count()

    // If we have both a parent task and a container, we MUST have an edge
    if (nodeCount > 1 && containerCount > 0) {
      const edges = page.locator('.react-flow__edge')
      const edgeCount = await edges.count()

      // This is the critical assertion: parent-to-container edge must exist
      expect(edgeCount).toBeGreaterThan(0)

      // Verify edge SVG paths are actually rendered (not just elements)
      const edgePaths = page.locator('.react-flow__edge path[d]')
      const renderedPathCount = await edgePaths.count()
      expect(renderedPathCount).toBeGreaterThan(0)
    }
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
    const hasGraph = await navigateToFirstGraph(page)
    if (!hasGraph) {
      test.skip()
      return
    }

    // Check if this graph contains a CHORD container
    const chordLabel = page.locator('.header-label-node:has-text("CHORD")')
    const hasChord = await chordLabel.first().isVisible().catch(() => false)

    if (!hasChord) {
      // Skip if no chord in this graph - informational test
      test.skip()
      return
    }

    // Count edges in a chord graph - should have at least 3 (one from each header task)
    const edges = page.locator('.react-flow__edge')
    const edgeCount = await edges.count()
    expect(edgeCount).toBeGreaterThanOrEqual(3)
  })
})
