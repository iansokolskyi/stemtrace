import type { Locator, Page } from '@playwright/test'

/**
 * Renderer-neutral locators for the flow graph. They read the `data-*` hooks
 * the graph carries (`flow-node`, `flow-edge`, `flow-container-label`), never the
 * renderer's own classes, so the same tests hold for any renderer that carries them.
 */

const NODE = '[data-testid="flow-node"]'
const CONTAINER_TYPES = '[data-node-type="GROUP"], [data-node-type="CHORD"]'

/** Every node the graph draws: tasks, and GROUP/CHORD containers. */
export function flowNodes(page: Page): Locator {
  return page.locator(NODE)
}

/** The node drawn for `taskId`. */
export function flowNode(page: Page, taskId: string): Locator {
  return page.locator(`${NODE}[data-task-id="${taskId}"]`)
}

/** Every edge. */
export function flowEdges(page: Page): Locator {
  return page.getByTestId('flow-edge')
}

/** The edge from `from` to `to`. */
export function flowEdge(page: Page, from: string, to: string): Locator {
  return page.locator(`[data-testid="flow-edge"][data-from="${from}"][data-to="${to}"]`)
}

/** GROUP and CHORD nodes. */
export function flowContainers(page: Page): Locator {
  return page.locator(`${NODE}:is(${CONTAINER_TYPES})`)
}

/** Nodes drawn inside a container (its members). */
export function flowContainerMembers(page: Page): Locator {
  return page.locator(`${NODE}[data-container-id]`)
}

/** Nodes drawn outside any container. */
export function flowTopLevelNodes(page: Page): Locator {
  return page.locator(`${NODE}:not([data-container-id])`)
}

/** The labels (GROUP or CHORD, and state) of the containers. */
export function flowContainerLabels(page: Page): Locator {
  return page.getByTestId('flow-container-label')
}
