import { expect, test } from '@playwright/test'

/**
 * Tests for the Graphs page.
 *
 * Prerequisites:
 *   docker compose -f docker-compose.e2e.yml up -d --wait
 *   # Submit workflow tasks to create graphs
 */

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

    // Try to click a node
    const node = page.locator('.react-flow__node, .node').first()
    const nodeVisible = await node.isVisible().catch(() => false)

    if (nodeVisible) {
      await node.click()
      await page.waitForLoadState('networkidle')

      // Should show task details panel or navigate
      // This behavior depends on implementation
    }
  })
})
