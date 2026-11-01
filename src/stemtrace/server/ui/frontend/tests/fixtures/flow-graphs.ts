import { readFileSync } from 'node:fs'
import type { Page } from '@playwright/test'

import type { GraphNode } from '../../src/api/client'

/**
 * Graphs as `GET /api/graphs/{root}` sends them: the captured ones (flow-graphs.json, written by
 * tests/data/export_flow_graphs.py from tests/data/celery_flows.json) and small synthetic ones,
 * plus a helper that serves one to a page.
 */

export interface FlowGraph {
  root_id: string
  nodes: Record<string, GraphNode>
}

const CAPTURES = JSON.parse(
  readFileSync(new URL('./flow-graphs.json', import.meta.url), 'utf8'),
) as Record<string, FlowGraph>

export function allCaptures(): Record<string, FlowGraph> {
  return CAPTURES
}

/** A copy of the graph captured under `name` (a test may change it). */
export function capture(name: string): FlowGraph {
  const found = CAPTURES[name]
  if (!found) throw new Error(`no capture ${name}`)
  return structuredClone(found)
}

let counter = 0

/** A node with the fields every graph node has; `overrides` say the rest. */
export function node(overrides: Partial<GraphNode> & { task_id?: string }): GraphNode {
  counter += 1
  const id = overrides.task_id ?? `t${counter}`
  return {
    task_id: id,
    name: 'app.step',
    state: 'SUCCESS',
    node_type: 'TASK',
    group_id: null,
    chord_id: null,
    parent_id: null,
    reported_parent_id: null,
    children: [],
    duration_ms: null,
    first_seen: null,
    last_updated: null,
    ...overrides,
  }
}

export function graphOf(...list: GraphNode[]): Record<string, GraphNode> {
  return Object.fromEntries(list.map((entry) => [entry.task_id, entry]))
}

/** A chain of `length` tasks: c0 -> c1 -> ... */
export function chain(length: number): Record<string, GraphNode> {
  const list = Array.from({ length }, (_, i) =>
    node({ task_id: `c${i}`, children: i + 1 < length ? [`c${i + 1}`] : [] }),
  )
  return graphOf(...list)
}

/** A container `g` of `count` members m0, m1, ...; `states[i]` overrides member i's state. */
export function group(
  count: number,
  states: Record<number, string> = {},
  type: 'GROUP' | 'CHORD' = 'GROUP',
): Record<string, GraphNode> {
  const members = Array.from({ length: count }, (_, i) =>
    node({
      task_id: `m${i}`,
      group_id: 'g',
      state: states[i] ?? 'SUCCESS',
      first_seen: new Date(2026, 0, 1, 0, 0, i).toISOString(),
    }),
  )
  const container = node({
    task_id: 'g',
    name: 'group',
    node_type: type,
    group_id: 'g',
    children: members.map((member) => member.task_id),
  })
  return graphOf(container, ...members)
}

/** What `GET /api/tasks/{id}` sends for one node: the node and its events. */
export interface TaskDetail {
  kwargs?: Record<string, unknown>
  exception?: string
}

/**
 * Makes the page's graph request answer with `graph` (and its task requests with `details`, the
 * rest 404). Call after `setupMockApi`: the last route registered answers first.
 */
export async function serveFlow(
  page: Page,
  graph: FlowGraph,
  details: Record<string, TaskDetail> = {},
): Promise<void> {
  await page.route(/\/api\/graphs\/[^/?]+(\?.*)?$/, (route) =>
    route.fulfill({ json: { root_id: graph.root_id, nodes: graph.nodes } }),
  )
  await page.route(/\/api\/tasks\/[^/?]+(\?.*)?$/, (route) => {
    const id = decodeURIComponent(new URL(route.request().url()).pathname.split('/').pop() ?? '')
    const found = graph.nodes[id]
    const detail = details[id]
    if (!found) return route.fulfill({ status: 404, json: { detail: 'not found' } })
    const event = (state: string, extra: Record<string, unknown> = {}) => ({
      task_id: id,
      name: found.name,
      state,
      timestamp: found.first_seen ?? '2026-01-01T00:00:00Z',
      parent_id: null,
      root_id: graph.root_id,
      group_id: found.group_id,
      trace_id: null,
      retries: 0,
      args: null,
      kwargs: null,
      result: null,
      exception: null,
      traceback: null,
      ...extra,
    })
    return route.fulfill({
      json: {
        task: {
          ...found,
          events: [
            event('PENDING', { kwargs: detail?.kwargs ?? null }),
            ...(detail?.exception ? [event('FAILURE', { exception: detail.exception })] : []),
          ],
        },
        children: [],
        flow_root_id: graph.root_id,
      },
    })
  })
}

/** The prototype's "Nightly reallocation": a root, then a chord of 48 whose header 2 failed, so the callback did not run. */
export function reallocationFlow(): FlowGraph {
  const root = node({
    task_id: 'root',
    name: 'reallocation.tasks.run_daily_reallocation',
    children: ['chord'],
    runtime_ms: 1200,
    hostname: 'celery@worker-realloc-1',
    queue: 'reallocation',
    queue_wait_ms: 40,
    first_seen: new Date(2026, 9, 16, 22, 0, 0).toISOString(),
  })
  const members = Array.from({ length: 48 }, (_, i) =>
    node({
      task_id: `p${i}`,
      name: 'reallocation.tasks.allocate_partner',
      group_id: 'chord',
      state: i === 2 ? 'FAILURE' : 'SUCCESS',
      exception_type: i === 2 ? 'PartnerAPIError' : null,
      runtime_ms: 800 + ((i * 37) % 900),
      hostname: 'celery@worker-realloc-2',
      queue: 'reallocation',
      first_seen: new Date(2026, 9, 16, 22, 0, 1, i * 20).toISOString(),
    }),
  )
  const chord = node({
    task_id: 'chord',
    name: 'chord',
    node_type: 'CHORD',
    state: 'FAILURE',
    group_id: 'chord',
    children: members.map((member) => member.task_id),
    not_run_reason: 'header_failed',
    duration_ms: 3900,
  })
  return { root_id: 'root', nodes: graphOf(root, chord, ...members) }
}

/** The prototype's "Order" flow: a chain of four tasks that all succeeded. */
export function checkoutFlow(): FlowGraph {
  const names = ['place_order', 'reserve_inventory', 'capture_payment', 'send_receipt']
  const list = names.map((name, i) =>
    node({
      task_id: `s${i}`,
      name: `checkout.tasks.${name}`,
      children: i < 3 ? [`s${i + 1}`] : [],
      runtime_ms: 300 + i * 120,
      hostname: 'celery@worker-checkout-1',
      queue: 'checkout',
      first_seen: new Date(2026, 9, 17, 11, 56, i).toISOString(),
    }),
  )
  return { root_id: 's0', nodes: graphOf(...list) }
}
