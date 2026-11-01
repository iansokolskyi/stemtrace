import { expect, type Locator, type Page, test, type WebSocketRoute } from '@playwright/test'

import {
  flowContainerLabels,
  flowContainerMembers,
  flowContainers,
  flowEdge,
  flowEdges,
  flowNode,
  flowNodes,
  flowTopLevelNodes,
} from './fixtures/flow'
import { type MockApiContext, setupMockApi } from './fixtures/mock-api'
import {
  createChain,
  createGroup,
  createSuccessTask,
  createWorkflowWithGroup,
  type MockTask,
} from './fixtures/mock-data'

/**
 * Tests for the Graphs page.
 *
 * These tests use mocked API responses, no Docker required.
 * Run with E2E_MODE=real for integration testing against Docker.
 */

const isRealMode = process.env.E2E_MODE === 'real'

/**
 * Open a graph's page by root id and wait until it has rendered a node. The wait is
 * bounded by the test's timeout rather than an assertion's 5 s, which a slow
 * first load of the dev server's modules can exceed (a blank page). It waits
 * for the node to be in the DOM, as the counts that follow do, not visible.
 */
async function openGraphById(page: import('@playwright/test').Page, rootId: string): Promise<void> {
  await page.goto(`/flows/${encodeURIComponent(rootId)}`)
  await flowNodes(page).first().waitFor({ state: 'attached' })
}

/**
 * Helper to navigate to first available graph.
 *
 * Waits for the graphs list to settle: either a graph link shows up or the page
 * says there are no graphs. Returns false only in the second case (genuinely no
 * graph data, so callers skip). If neither appears (slow load, failed request)
 * the wait times out and the test fails instead of silently skipping.
 */
async function navigateToFirstGraph(page: import('@playwright/test').Page): Promise<boolean> {
  await page.goto('/flows')

  const graphLink = page.locator('a[href*="/flows/"]').first()
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
 * Open a specific graph, chosen from the API's graph list.
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

  // Open the graph by its route instead of finding its link: the /flows list shows
  // one page, and on a reused stack the match can be on a later one. Navigating
  // from the list is covered by "can navigate to graph detail".
  await openGraphById(page, match.task_id)
}

test.describe('Graphs Page', () => {
  test.beforeEach(async ({ page }) => {
    if (!isRealMode) {
      await setupMockApi(page)
    }
  })

  test('displays graphs list', async ({ page }) => {
    await page.goto('/flows')
    await page.waitForLoadState('networkidle')

    // Page should load without errors
    const heading = page.locator('h1, h2, [class*="heading"]')
    await expect(heading.first()).toBeVisible()
  })

  test('shows graph entries when workflows exist', async ({ page }) => {
    await page.goto('/flows')
    await page.waitForLoadState('networkidle')

    // The list loads after the page does
    await expect(page.locator('a[href*="/flows/"]').first()).toBeVisible({ timeout: 15_000 })
  })

  test('can navigate to graph detail', async ({ page }) => {
    const hasGraph = await navigateToFirstGraph(page)
    if (!hasGraph) {
      test.skip()
      return
    }
    await expect(page).toHaveURL(/\/flows\/.+/)
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

    // The graph is drawn: its first node is visible
    await expect(flowNodes(page).first()).toBeVisible()
  })

  test('shows graph nodes', async ({ page }) => {
    const hasGraph = await navigateToFirstGraph(page)
    if (!hasGraph) {
      test.skip()
      return
    }
    await page.waitForLoadState('networkidle')

    // Look for graph nodes
    const nodes = flowNodes(page)

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

    // Every node says its state
    await expect(flowNodes(page).first()).toBeVisible()
    const states = await flowNodes(page).evaluateAll((els) =>
      els.map((el) => el.getAttribute('data-state')),
    )
    expect(states.length).toBeGreaterThan(0)
    for (const state of states) expect(state).toMatch(/^[A-Z]+$/)
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
    const taskLink = flowNodes(page).locator('a[href*="/tasks/"]').first()
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

    const edges = flowEdges(page)

    // Nodes and edges render after the page's data loads and the graph is laid out,
    // so wait for the first node instead of counting whatever is in the DOM right now
    const nodes = flowNodes(page)
    await expect(nodes.first()).toBeVisible()
    const nodeCount = await nodes.count()

    if (nodeCount > 1) {
      // With multiple nodes, we expect at least one edge or visible edge paths;
      // poll because edges are drawn a moment after their nodes
      await expect
        .poll(() => edges.count(), { message: 'multi-node graph should render at least one edge' })
        .toBeGreaterThan(0)
    }
  })

  test('edges connect parent to child nodes', async ({ page }) => {
    await openGraph(page, mockApi, PARENT_WITH_GROUP_GRAPH)

    const edges = flowEdges(page)
    await expect(edges.first()).toBeVisible()
    expect(await edges.count()).toBeGreaterThan(0)
  })

  test('edges have appropriate styling', async ({ page }) => {
    await openGraph(page, mockApi, PARENT_WITH_GROUP_GRAPH)
    await expect(flowEdges(page).first()).toBeAttached()

    // Look for SVG path elements within the edges
    const edgePaths = flowEdges(page).locator('path')
    const pathCount = await edgePaths.count()

    // A graph with several nodes has edges, and each is drawn as a path
    expect(pathCount).toBeGreaterThan(0)

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

    const containerNodes = flowContainers(page)
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

    const containerNodes = flowContainers(page)
    await expect(containerNodes.first()).toBeVisible()

    // Container nodes should have task nodes rendered inside them: data-container-id
    // marks the task nodes inside a container. The chord has 3 members plus a callback.
    const childNodes = flowContainerMembers(page)
    await expect(childNodes.first()).toBeVisible()
    expect(await childNodes.count()).toBeGreaterThanOrEqual(1)
  })

  test('container header shows GROUP or CHORD label', async ({ page }) => {
    await openGraph(page, mockApi, CHORD_GRAPH)

    const label = flowContainerLabels(page).first()
    await expect(label).toBeVisible()
    await expect(label).toContainText('CHORD')
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

    // The graph has a parent task, a GROUP container and members, so there are
    // several nodes and exactly one container
    const nodes = flowNodes(page)
    await expect(nodes.first()).toBeVisible()
    expect(await nodes.count()).toBeGreaterThan(1)
    await expect(flowContainers(page)).toHaveCount(1)

    // This is the critical assertion: parent-to-container edge must exist, and its
    // SVG path must actually be rendered (not just the element). Poll: edges are
    // drawn a moment after their nodes.
    await expect
      .poll(() => flowEdges(page).count(), {
        message: 'parent-to-container edge should be rendered',
      })
      .toBeGreaterThan(0)
    await expect
      .poll(() => flowEdges(page).locator('path[d]').count(), {
        message: 'edge SVG paths should be rendered',
      })
      .toBeGreaterThan(0)
  })

  /**
   * Regression test: Edges from parent task to GROUP container must be visible.
   * This catches the issue where GROUP containers were missing incoming edges.
   */
  test('parent-to-container edges are rendered', async ({ page }) => {
    await openGraph(page, mockApi, PARENT_WITH_GROUP_GRAPH)

    // The graph's one container is the target of an edge (drawn a moment after its node)
    const container = flowContainers(page)
    await expect(container).toHaveCount(1)
    const containerId = (await container.getAttribute('data-task-id'))!
    await expect(page.locator(`[data-testid="flow-edge"][data-to="${containerId}"]`)).toHaveCount(1)
  })

  /**
   * Regression test: CHORD callback edges must connect from ALL header tasks to callback.
   * Each header task contributes to triggering the callback.
   */
  test('chord has edges from all header tasks to callback', async ({ page }) => {
    await openGraph(page, mockApi, CHORD_GRAPH)

    await expect(flowContainerLabels(page).filter({ hasText: 'CHORD' }).first()).toBeVisible()

    // A chord with 3 header tasks has at least 3 edges (one from each header task
    // to the callback). Poll: edges are drawn a moment after their nodes.
    await expect
      .poll(() => flowEdges(page).count(), {
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

    await page.goto('/flows')
    await page.waitForLoadState('networkidle')

    // Should show the group in the list
    const graphLink = page.locator('a[href*="/flows/"]').first()
    await expect(graphLink).toBeVisible()

    await graphLink.click()
    await page.waitForLoadState('networkidle')

    // Should have 4 nodes: 1 GROUP container + 3 members
    // The graph is laid out after its data arrives: wait for the count, not a snapshot of the DOM
    const nodes = flowNodes(page)
    await expect(nodes).toHaveCount(4)
  })

  test('renders workflow with parent spawning group', async ({ page }) => {
    const mockApi = await setupMockApi(page, { useDefaults: false })
    const workflowTasks = createWorkflowWithGroup(2)
    mockApi.addTasks(workflowTasks)

    await page.goto('/flows')
    await page.waitForLoadState('networkidle')

    // Find the batch_processor graph
    const graphLink = page.getByText('tasks.batch_processor').first()
    await expect(graphLink).toBeVisible()

    await graphLink.click()
    await page.waitForLoadState('networkidle')

    // Should have nodes: 1 parent + 1 GROUP container + 2 members = 4 nodes
    // The graph is laid out after its data arrives: wait for the count, not a snapshot of the DOM
    const nodes = flowNodes(page)
    await expect(nodes).toHaveCount(4)

    // The edge runs from the parent to the group (drawn a moment after their nodes)
    const [parent, groupNode] = workflowTasks
    await expect(flowEdge(page, parent.task_id, groupNode.task_id)).toHaveCount(1)
    await expect(flowEdges(page)).toHaveCount(1)
  })

  test('renders chord with callback', async ({ page }) => {
    const mockApi = await setupMockApi(page, { useDefaults: false })
    const chordTasks = createGroup(3, { withCallback: true })
    // A member that is not SUCCESS, so a constant data-state cannot pass
    chordTasks[1].state = 'FAILURE'
    mockApi.addTasks(chordTasks)

    await page.goto('/flows')
    await page.waitForLoadState('networkidle')

    const graphLink = page.locator('a[href*="/flows/"]').first()
    await graphLink.click()
    await page.waitForLoadState('networkidle')

    // 1 CHORD container + 3 members + 1 callback
    await expect(flowNodes(page)).toHaveCount(5)
    // One edge from each header task to the callback
    await expect(flowEdges(page)).toHaveCount(3)

    // The hooks say what each node and edge is
    const [chordNode, ...rest] = chordTasks
    const callback = rest[rest.length - 1]
    const members = rest.slice(0, -1)
    await expect(flowNode(page, chordNode.task_id)).toHaveAttribute('data-node-type', 'CHORD')
    await expect(flowNode(page, callback.task_id)).toHaveAttribute('data-node-type', 'TASK')
    await expect(flowNode(page, callback.task_id)).toHaveAttribute('data-state', callback.state)
    await expect(flowNode(page, callback.task_id)).not.toHaveAttribute('data-container-id')
    await expect(flowNode(page, members[0].task_id)).toHaveAttribute('data-state', 'FAILURE')
    await expect(flowNode(page, members[1].task_id)).toHaveAttribute('data-state', 'SUCCESS')
    for (const member of members) {
      await expect(flowNode(page, member.task_id)).toHaveAttribute(
        'data-container-id',
        chordNode.task_id,
      )
      await expect(flowEdge(page, member.task_id, callback.task_id)).toHaveCount(1)
    }
    await expect(flowContainerLabels(page)).toHaveAttribute('data-container-id', chordNode.task_id)

    // CHORD label should be visible
    const chordText = page.getByText('CHORD')
    await expect(chordText.first()).toBeVisible()
  })
})

test.describe('Graph Page - Chains In Groups', () => {
  test.skip(isRealMode, 'Mock-only test')

  test('draws each chain of a group of chains as a row inside the group', async ({ page }) => {
    // group(chain(a1, b1), chain(a2, b2)): the server puts a1 and a2 (chain
    // links) inside the group, with b1 and b2 (the members) after them
    const mockApi = await setupMockApi(page, { useDefaults: false })
    const [groupNode, b1, b2] = createGroup(2, { withCallback: false })
    const a1 = createSuccessTask('tasks.first_step')
    const a2 = createSuccessTask('tasks.first_step')
    for (const [link, member] of [
      [a1, b1],
      [a2, b2],
    ]) {
      link.parent_id = groupNode.task_id
      link.root_id = groupNode.task_id
      link.children = [member.task_id]
      member.parent_id = link.task_id
    }
    groupNode.children = [a1.task_id, a2.task_id]
    mockApi.addTasks([groupNode, a1, a2, b1, b2])

    await openGraphById(page, groupNode.task_id)

    // 1 GROUP container + 4 tasks, all inside the container
    await expect(flowNodes(page)).toHaveCount(5)
    await expect(flowContainerMembers(page)).toHaveCount(4)
    // a1 -> b1 and a2 -> b2
    await expect(flowEdges(page)).toHaveCount(2)
    await expect(flowEdge(page, a1.task_id, b1.task_id)).toHaveCount(1)
    await expect(flowEdge(page, a2.task_id, b2.task_id)).toHaveCount(1)
    for (const task of [a1, a2, b1, b2]) {
      await expect(flowNode(page, task.task_id)).toHaveAttribute(
        'data-container-id',
        groupNode.task_id,
      )
    }
  })
})

test.describe('Graph Page - Container Cell Children', () => {
  test.skip(isRealMode, 'Mock-only test')

  // As the server sends the chord_header_subtask capture: chord([a, b], cb)
  // where header a also sends a subtask of its own
  test('draws a chord header subtask outside the chord', async ({ page }) => {
    const mockApi = await setupMockApi(page, { useDefaults: false })
    const [chordNode, ...rest] = createGroup(2, { withCallback: true })
    const [header] = rest
    const subtask = createSuccessTask('tasks.subtask')
    subtask.parent_id = header.task_id
    subtask.root_id = chordNode.task_id
    header.children = [subtask.task_id]
    mockApi.addTasks([chordNode, ...rest, subtask])

    await openGraphById(page, chordNode.task_id)

    // CHORD container + 2 headers + callback + subtask
    await expect(flowNodes(page)).toHaveCount(5)
    await expect(flowNode(page, subtask.task_id)).toHaveCount(1)
    // header -> subtask, and each header -> callback
    await expect(flowEdges(page)).toHaveCount(3)
    await expect(flowEdge(page, header.task_id, subtask.task_id)).toHaveCount(1)
    await expect(flowEdge(page, rest[1].task_id, rest[2].task_id)).toHaveCount(1)
  })

  test('draws a group a header starts once, as its own container', async ({ page }) => {
    const mockApi = await setupMockApi(page, { useDefaults: false })
    const [chordNode, ...rest] = createGroup(2, { withCallback: true })
    const [header] = rest
    const [innerGroup, ...innerMembers] = createGroup(2, { withCallback: false })
    innerGroup.parent_id = header.task_id
    header.children = [innerGroup.task_id]
    for (const task of [innerGroup, ...innerMembers]) task.root_id = chordNode.task_id
    mockApi.addTasks([chordNode, ...rest, innerGroup, ...innerMembers])

    await openGraphById(page, chordNode.task_id)

    // Each container, 2 chord headers, the callback and
    // 2 inner members: every node exactly once
    await expect(flowNodes(page)).toHaveCount(7)
    await expect(flowNode(page, innerGroup.task_id)).toHaveCount(1)
  })
})

test.describe('Graph Page - Layout', () => {
  test.skip(isRealMode, 'Mock-only test')

  test('sibling chords with header subtasks do not overlap', async ({ page }, testInfo) => {
    const mockApi = await setupMockApi(page, { useDefaults: false })
    const parent = createSuccessTask('tasks.parent')
    const all = [parent]
    for (let i = 0; i < 3; i++) {
      const [chordNode, ...rest] = createGroup(2, { withCallback: true })
      const [header] = rest
      const subtask = createSuccessTask('tasks.subtask')
      subtask.parent_id = header.task_id
      header.children = [subtask.task_id]
      chordNode.parent_id = parent.task_id
      parent.children.push(chordNode.task_id)
      for (const task of [chordNode, ...rest, subtask]) task.root_id = parent.task_id
      all.push(chordNode, ...rest, subtask)
    }
    mockApi.addTasks(all)

    await openGraphById(page, parent.task_id)
    // parent + 3 x (container + 2 headers + callback + subtask)
    await expect(flowNodes(page)).toHaveCount(16)
    await page.screenshot({ path: testInfo.outputPath('sibling-chords.png'), fullPage: true })

    // Top-level nodes (containers, tasks, callbacks, subtasks) never overlap
    const boxes = []
    for (const node of await flowTopLevelNodes(page).all()) {
      const box = await node.boundingBox()
      if (box) boxes.push({ id: await node.getAttribute('data-task-id'), ...box })
    }
    // parent, 3 containers, 3 callbacks, 3 subtasks
    expect(boxes).toHaveLength(10)
    for (const [i, a] of boxes.entries()) {
      for (const b of boxes.slice(i + 1)) {
        const overlap =
          a.x < b.x + b.width && b.x < a.x + a.width && a.y < b.y + b.height && b.y < a.y + a.height
        expect(overlap, `${a.id} overlaps ${b.id}`).toBe(false)
      }
    }
  })
})

test.describe('Graph Page - Chord Bodies', () => {
  test.skip(isRealMode, 'Mock-only test')

  test('draws a chord whose body is a group as a container after it', async ({ page }) => {
    // As the server sends chord([a, b], group(c, d)): the body tasks carry
    // the chord's group ID and sit in a "group:<id>:body" GROUP node, which
    // the CHORD names as its callback (chord_id)
    const mockApi = await setupMockApi(page, { useDefaults: false })
    const [chordNode, ...headers] = createGroup(2, { withCallback: false })
    const groupId = chordNode.group_id as string
    const [, ...bodyTasks] = createGroup(2, { withCallback: false })
    const body = {
      ...chordNode,
      task_id: `group:${groupId}:body`,
      name: 'group',
      node_type: 'GROUP' as const,
      parent_id: chordNode.task_id,
      children: bodyTasks.map((t) => t.task_id),
    }
    for (const task of bodyTasks) {
      task.group_id = groupId
      task.parent_id = body.task_id
      task.root_id = chordNode.task_id
    }
    chordNode.node_type = 'CHORD'
    chordNode.chord_id = body.task_id
    chordNode.children.push(body.task_id)
    mockApi.addTasks([chordNode, ...headers, body, ...bodyTasks])

    await openGraphById(page, chordNode.task_id)

    // Each container, 2 headers, 2 body tasks
    await expect(flowNodes(page)).toHaveCount(6)
    await expect(flowNode(page, body.task_id)).toHaveCount(1)
    // Each header -> the body container
    await expect(flowEdges(page)).toHaveCount(2)
    // The body container sits after the chord, to its right
    const chordBox = await flowNode(page, chordNode.task_id).boundingBox()
    const bodyBox = await flowNode(page, body.task_id).boundingBox()
    expect(chordBox).not.toBeNull()
    expect(bodyBox).not.toBeNull()
    if (chordBox && bodyBox) {
      expect(bodyBox.x).toBeGreaterThan(chordBox.x + chordBox.width)
    }
    // The body tasks are drawn inside the body container
    for (const task of bodyTasks) {
      const taskBox = await flowNode(page, task.task_id).boundingBox()
      expect(taskBox).not.toBeNull()
      if (taskBox && bodyBox) {
        expect(taskBox.x).toBeGreaterThanOrEqual(bodyBox.x)
        expect(taskBox.x + taskBox.width).toBeLessThanOrEqual(bodyBox.x + bodyBox.width + 1)
      }
    }
  })

  test('draws a chord whose body is another chord as a container', async ({ page }) => {
    const mockApi = await setupMockApi(page, { useDefaults: false })
    const [outer, ...outerRest] = createGroup(2, { withCallback: false })
    const [inner, ...innerRest] = createGroup(2, { withCallback: true })
    const innerCallback = innerRest[innerRest.length - 1]
    // As the server sends `chord([a, b], chord([c, d], e))`: the outer CHORD's
    // callback (chord_id) is the inner CHORD node
    outer.node_type = 'CHORD'
    outer.chord_id = inner.task_id
    outer.children.push(inner.task_id)
    inner.parent_id = outer.task_id
    for (const task of [inner, ...innerRest]) task.root_id = outer.task_id
    mockApi.addTasks([outer, ...outerRest, inner, ...innerRest])

    await openGraphById(page, outer.task_id)

    // Each CHORD: container + 2 headers; plus the inner callback
    await expect(flowNodes(page)).toHaveCount(7)
    await expect(flowNode(page, innerCallback.task_id)).toHaveCount(1)
    // 2 outer headers -> inner CHORD, 2 inner headers -> inner callback
    await expect(flowEdges(page)).toHaveCount(4)
  })
})

test.describe('Graph Page - Malformed Links', () => {
  // The server never sends these shapes; the page must still render them
  test.skip(isRealMode, 'Mock-only test')

  test('renders a group listed among its own children', async ({ page }) => {
    const mockApi = await setupMockApi(page, { useDefaults: false })
    const [groupNode, ...members] = createGroup(2, { withCallback: false })
    groupNode.parent_id = groupNode.task_id
    groupNode.children = [groupNode.task_id, ...groupNode.children]
    mockApi.addTasks([groupNode, ...members])

    await openGraphById(page, groupNode.task_id)

    // 1 GROUP container + 2 members
    await expect(flowNodes(page)).toHaveCount(3)
  })

  test('renders tasks whose children form a cycle', async ({ page }) => {
    const mockApi = await setupMockApi(page, { useDefaults: false })
    const first = createSuccessTask('tasks.first')
    const second = createSuccessTask('tasks.second')
    second.root_id = first.task_id
    first.children = [second.task_id]
    second.children = [first.task_id]
    second.parent_id = first.task_id
    first.parent_id = second.task_id
    mockApi.addTasks([first, second])

    await openGraphById(page, first.task_id)

    await expect(flowNodes(page)).toHaveCount(2)
  })

  test('draws a chord callback once when its parent lists it first', async ({ page }) => {
    // The server sends this shape when the callback reported its parent
    // before the chord was created under the same parent
    const mockApi = await setupMockApi(page, { useDefaults: false })
    const [chordNode, ...rest] = createGroup(2, { withCallback: true })
    const callback = rest[rest.length - 1]
    const parent = createSuccessTask('tasks.parent')
    parent.children = [callback.task_id, chordNode.task_id]
    chordNode.parent_id = parent.task_id
    callback.parent_id = parent.task_id
    for (const task of [chordNode, ...rest]) task.root_id = parent.task_id
    mockApi.addTasks([parent, chordNode, ...rest])
    const messages: string[] = []
    page.on('console', (msg) => {
      if (msg.type() === 'warning' || msg.type() === 'error') messages.push(msg.text())
    })

    await openGraphById(page, parent.task_id)

    // parent + CHORD container + 2 headers + callback
    await expect(flowNodes(page)).toHaveCount(5)
    await expect(flowNode(page, callback.task_id)).toHaveCount(1)
    // parent -> callback, parent -> chord, and one edge per header -> callback
    await expect(flowEdges(page)).toHaveCount(4)
    expect(messages.filter((text) => /key|duplicate|React Flow/i.test(text))).toEqual([])
  })
})

/**
 * Opens a graph with the live socket mocked and returns a function that pushes
 * an event, as the server does after each change to the graph. The page then
 * refetches the graph from the API, and the returned function resolves once that
 * response is in, so a test that still sees the old graph is looking at a page
 * that did not draw the new data. Resolves once the page reports "Live".
 */
async function openLiveGraph(page: Page, rootId: string): Promise<LiveGraph> {
  const sockets: WebSocketRoute[] = []
  await page.routeWebSocket(/\/ws$/, (ws) => {
    sockets.push(ws)
  })
  await openGraphById(page, rootId)
  await expect(page.getByText('Live', { exact: true })).toBeVisible()
  // StrictMode (dev) opens and drops a socket before the one that goes live
  const ws = sockets[sockets.length - 1]
  const emit = (task: MockTask) =>
    ws.send(
      JSON.stringify({
        task_id: task.task_id,
        name: task.name,
        state: task.state,
        timestamp: new Date().toISOString(),
        parent_id: task.parent_id,
        root_id: task.root_id,
        group_id: null,
        trace_id: null,
        retries: 0,
      }),
    )
  const push = async (task: MockTask) => {
    const refetched = page.waitForResponse(
      (response) =>
        response.request().resourceType() === 'fetch' &&
        decodeURIComponent(new URL(response.url()).pathname).endsWith(`/api/graphs/${rootId}`),
    )
    emit(task)
    await refetched
  }
  return { emit, push }
}

/**
 * The page's live socket, as the server uses it: `emit` sends one event;
 * `push` sends one and resolves once the page has refetched the graph.
 */
interface LiveGraph {
  emit: (task: MockTask) => void
  push: (task: MockTask) => Promise<void>
}

/** The canvas the graph is drawn on. */
function graphCanvas(page: Page) {
  return page.getByTestId('flow-canvas')
}

/** A locator's bounding box once it has stopped changing (fits and zooms take a few frames). */
async function settledBox(locator: Locator) {
  let last = ''
  await expect
    .poll(
      async () => {
        const now = JSON.stringify(await locator.boundingBox())
        const settled = now === last
        last = now
        return settled
      },
      { intervals: [150], timeout: 10_000 },
    )
    .toBe(true)
  return JSON.parse(last) as { x: number; y: number; width: number; height: number }
}

/**
 * The viewport's transform once it has stopped changing: wheel zooms and fits
 * take a few frames, so one read can catch them half way.
 */
async function settledViewport(page: Page): Promise<string> {
  // xyflow-only: removed with the renderer in PR 4c
  const viewport = page.locator('.react-flow__viewport')
  let last: string | null = null
  await expect
    .poll(
      async () => {
        const now = await viewport.getAttribute('style')
        const settled = now === last
        last = now
        return settled
      },
      { intervals: [150], timeout: 10_000 },
    )
    .toBe(true)
  return last ?? ''
}

// Chromium's intersectionRatio is a float: a fully visible node at a fractional
// position (the fitted zoom is rarely a whole number) reports 0.9999997, not 1.
const FULLY_VISIBLE = 0.99

test.describe('Graph Page - Live Updates', () => {
  test.skip(isRealMode, 'Mock-only test')

  /** step_1 -> step_2 (running), as the store holds it before the update. */
  function runningChain() {
    const [first, second] = createChain('app.flow', 2)
    second.state = 'STARTED'
    return { first, second }
  }

  /** The store after step_2 finished and spawned step_3. */
  function finishStepTwo(mockApi: MockApiContext, first: MockTask, second: MockTask): MockTask {
    const [, , third] = createChain('app.flow', 3)
    third.task_id = `${first.task_id.slice(0, -4)}ffff`
    third.root_id = first.task_id
    third.parent_id = second.task_id
    third.state = 'STARTED'
    second.state = 'SUCCESS'
    second.children.push(third.task_id)
    mockApi.addTask(third)
    return third
  }

  test('shows a new task and a state change without a reload', async ({ page }) => {
    const mockApi = await setupMockApi(page, { useDefaults: false })
    const { first, second } = runningChain()
    mockApi.addTasks([first, second])
    const { push } = await openLiveGraph(page, first.task_id)
    await expect(flowNodes(page)).toHaveCount(2)
    await expect(flowNode(page, second.task_id)).toContainText('STARTED')
    await expect(flowNode(page, second.task_id)).toHaveAttribute('data-state', 'STARTED')
    await expect(flowNode(page, first.task_id)).toHaveAttribute('data-state', 'SUCCESS')

    const third = finishStepTwo(mockApi, first, second)
    await push(third)

    await expect(flowNode(page, third.task_id)).toContainText('step_3')
    // Nobody panned or zoomed, so the view re-fits to show the new task
    await expect(flowNode(page, third.task_id)).toBeInViewport({ ratio: FULLY_VISIBLE })
    await expect(flowNodes(page)).toHaveCount(3)
    await expect(flowEdges(page)).toHaveCount(2)
    await expect(flowEdge(page, first.task_id, second.task_id)).toHaveCount(1)
    await expect(flowEdge(page, second.task_id, third.task_id)).toHaveCount(1)
    await expect(flowNode(page, second.task_id)).toContainText('SUCCESS')
    await expect(flowNode(page, second.task_id)).toHaveAttribute('data-state', 'SUCCESS')
    await expect(flowNode(page, second.task_id)).not.toContainText('STARTED')
    await expect(flowNode(page, third.task_id)).toHaveAttribute('data-state', 'STARTED')
  })

  test('shows a new task and a state change by polling while the socket is down', async ({
    page,
  }) => {
    const mockApi = await setupMockApi(page, { useDefaults: false })
    const { first, second } = runningChain()
    mockApi.addTasks([first, second])
    // A socket the server closes at once: the page falls back to polling
    await page.routeWebSocket(/\/ws$/, (ws) => ws.close())
    await openGraphById(page, first.task_id)
    await expect(page.getByText('Polling', { exact: true })).toBeVisible()
    await expect(flowNode(page, second.task_id)).toContainText('STARTED')

    const third = finishStepTwo(mockApi, first, second)

    // The page polls every 5 s
    await expect(flowNode(page, third.task_id)).toContainText('step_3', { timeout: 15_000 })
    await expect(flowNode(page, second.task_id)).toContainText('SUCCESS')
  })

  // xyflow-only: removed with the renderer in PR 4c
  test('keeps a node the user dragged where it was left', async ({ page }) => {
    const mockApi = await setupMockApi(page, { useDefaults: false })
    const { first, second } = runningChain()
    mockApi.addTasks([first, second])
    const { push } = await openLiveGraph(page, first.task_id)
    const dragged = flowNode(page, first.task_id)
    const before = (await dragged.boundingBox())!
    // Drag by the card's id line, not the link text (a click would navigate)
    await page.mouse.move(before.x + 4, before.y + 4)
    await page.mouse.down()
    await page.mouse.move(before.x + 4, before.y + 124, { steps: 8 })
    await page.mouse.up()
    await expect.poll(async () => (await dragged.boundingBox())!.y).toBeGreaterThan(before.y + 50)
    // The node's own position (flow coordinates), unlike its box on screen,
    // does not change when the page re-fits the view for the new task
    const position = async () => dragged.evaluate((el) => (el as HTMLElement).style.transform)
    const moved = await position()

    const third = finishStepTwo(mockApi, first, second)
    await push(third)
    await expect(flowNode(page, third.task_id)).toBeVisible()

    expect(await position()).toBe(moved)
  })

  test('keeps re-fitting after a click on the empty canvas', async ({ page }) => {
    const mockApi = await setupMockApi(page, { useDefaults: false })
    const { first, second } = runningChain()
    mockApi.addTasks([first, second])
    const { push } = await openLiveGraph(page, first.task_id)
    const node = flowNode(page, first.task_id)
    const fitted = await settledBox(node)
    // A click is a mouse down and up on the canvas: no pan, no zoom
    const box = (await graphCanvas(page).boundingBox())!
    const [x, y] = [box.x + box.width - 40, box.y + 40]
    // The point is empty canvas, not a node
    expect(
      await page.evaluate(
        ([px, py]) => !!document.elementFromPoint(px, py)?.closest('[data-testid="flow-node"]'),
        [x, y],
      ),
    ).toBe(false)
    await page.mouse.click(x, y)
    expect(await settledBox(node)).toEqual(fitted)

    const third = finishStepTwo(mockApi, first, second)
    await push(third)

    await expect(flowNode(page, third.task_id)).toBeInViewport({ ratio: FULLY_VISIBLE })
  })

  // xyflow-only: removed with the renderer in PR 4c
  test('shows a new task in view once its size is known, however late that is', async ({
    page,
  }) => {
    // React Flow learns a node's size from a ResizeObserver: report it late
    await page.addInitScript(() => {
      const Native = window.ResizeObserver
      window.ResizeObserver = class extends Native {
        constructor(callback: ResizeObserverCallback) {
          super((entries, observer) => {
            setTimeout(() => callback(entries, observer), 600)
          })
        }
      }
    })
    const mockApi = await setupMockApi(page, { useDefaults: false })
    const { first, second } = runningChain()
    mockApi.addTasks([first, second])
    const { push } = await openLiveGraph(page, first.task_id)

    const third = finishStepTwo(mockApi, first, second)
    await push(third)
    // A second update lands before the first new node's size is known
    const [, , , fourth] = createChain('app.flow', 4)
    fourth.task_id = `${first.task_id.slice(0, -4)}eeee`
    fourth.root_id = first.task_id
    fourth.parent_id = third.task_id
    third.children.push(fourth.task_id)
    mockApi.addTask(fourth)
    await push(fourth)

    // A node is hidden until it is measured, and only then does a re-fit count: before
    // that the view is the unfitted one, which shows a node this near the origin
    await expect(flowNode(page, fourth.task_id)).toBeVisible({ timeout: 10_000 })
    await settledViewport(page)
    await expect(flowNode(page, third.task_id)).toBeInViewport({ ratio: 0.95 })
    await expect(flowNode(page, fourth.task_id)).toBeInViewport({ ratio: 0.95 })
  })

  // xyflow-only: removed with the renderer in PR 4c
  test('leaves a node under the cursor while an update arrives mid-drag', async ({ page }) => {
    const mockApi = await setupMockApi(page, { useDefaults: false })
    const { first, second } = runningChain()
    mockApi.addTasks([first, second])
    const { push } = await openLiveGraph(page, first.task_id)
    const held = flowNode(page, first.task_id)
    const position = async () => held.evaluate((el) => (el as HTMLElement).style.transform)
    const fitted = await settledViewport(page)
    const before = (await held.boundingBox())!
    await page.mouse.move(before.x + 4, before.y + 4)
    await page.mouse.down()
    await page.mouse.move(before.x + 4, before.y + 124, { steps: 8 })
    await expect.poll(async () => (await held.boundingBox())!.y).toBeGreaterThan(before.y + 50)
    const dragged = await position()

    // The mouse button is still down
    const third = finishStepTwo(mockApi, first, second)
    await push(third)
    await expect(flowNode(page, third.task_id)).toBeVisible()

    expect(await position()).toBe(dragged)
    expect(await settledViewport(page)).toBe(fitted)
    await page.mouse.up()
    // Released: the node stays where it was dropped, and the view may now re-fit
    expect(await position()).toBe(dragged)
    await expect(flowNode(page, third.task_id)).toBeInViewport({ ratio: FULLY_VISIBLE })
  })

  test('keeps the zoom the user chose with the zoom button', async ({ page }) => {
    // A chain too long to fit at full zoom, so the fitted view can still zoom in
    const mockApi = await setupMockApi(page, { useDefaults: false })
    const chain = createChain('app.flow', 7)
    const first = chain[0]
    const last = chain[chain.length - 1]
    last.state = 'STARTED'
    mockApi.addTasks(chain)
    const { push } = await openLiveGraph(page, first.task_id)
    const node = flowNode(page, first.task_id)
    const fitted = await settledBox(node)
    await page.getByRole('button', { name: /zoom in/i }).click()
    const zoomed = await settledBox(node)
    expect(zoomed).not.toEqual(fitted)

    // The last step finishes and spawns another
    const [, , , , , , next] = createChain('app.flow', 7)
    next.task_id = `${first.task_id.slice(0, -4)}ffff`
    next.root_id = first.task_id
    next.parent_id = last.task_id
    next.state = 'STARTED'
    last.state = 'SUCCESS'
    last.children.push(next.task_id)
    mockApi.addTask(next)
    await push(next)
    // A re-fit (the behaviour under test) would move the view once the new node is drawn
    await expect(flowNode(page, next.task_id)).toBeVisible()

    expect(await settledBox(node)).toEqual(zoomed)
  })

  // xyflow-only: removed with the renderer in PR 4c
  test('keeps the zoom the user chose with the wheel', async ({ page }) => {
    const mockApi = await setupMockApi(page, { useDefaults: false })
    const { first, second } = runningChain()
    mockApi.addTasks([first, second])
    const { push } = await openLiveGraph(page, first.task_id)
    const box = (await page.locator('.react-flow').boundingBox())!
    const fitted = await settledViewport(page)
    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2)
    // Zoom out: the fitted view of two nodes is already at the maximum zoom
    await page.mouse.wheel(0, 400)
    const zoomed = await settledViewport(page)
    expect(zoomed).not.toBe(fitted)

    const third = finishStepTwo(mockApi, first, second)
    await push(third)
    // A node is visible once React Flow has measured it, which is when a
    // re-fit (the behaviour under test) would have moved the view
    await expect(flowNode(page, third.task_id)).toBeVisible()

    expect(await settledViewport(page)).toBe(zoomed)
  })
})
