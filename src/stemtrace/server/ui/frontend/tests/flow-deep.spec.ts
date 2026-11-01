import { expect, test } from '@playwright/test'

import type { GraphNode } from '../src/api/client'
import { layoutFlow } from '../src/components/flow/layoutFlow'
import { buildTimeline, criticalPath } from '../src/components/flow/timeline'
import { graphOf, node } from './fixtures/flow-graphs'

/**
 * The Flow page on flows that are deep, not wide: a chain of 10,000 tasks is a real flow (the
 * server keeps 10,000 tasks by default). Every walk over the graph in components/flow must be
 * iterative: a recursive one throws `RangeError: Maximum call stack size exceeded` somewhere
 * between 1,000 and 5,000 levels. No browser: layout and timeline are pure.
 */

const T0 = Date.UTC(2026, 0, 1)
const iso = (ms: number): string => new Date(T0 + ms).toISOString()
const LENGTHS = [1000, 5000, 10_000, 20_000]

/** A finished task whose `step`-th slot is 10 ms after the one before, so the last one ends last. */
function timed(step: number, overrides: Partial<GraphNode> & { task_id: string }): GraphNode {
  return node({
    first_seen: iso(step * 10),
    last_updated: iso(step * 10 + 5),
    queue_wait_ms: 0,
    runtime_ms: 5,
    total_runtime_ms: 5,
    ...overrides,
  })
}

interface Shape {
  name: string
  build: (length: number) => { nodes: Record<string, GraphNode>; root: string }
  /** How many tasks and containers the layout draws, and the id the critical path ends at. */
  tasks: (length: number) => number
  containers: (length: number) => number
  last: (length: number) => string
  /** The indent of the last row: a child is one level below its parent, a container's members share its level. */
  depth: (length: number) => number
}

const SHAPES: Shape[] = [
  {
    name: 'a chain of tasks',
    build: (length) => ({
      root: 'c0',
      nodes: graphOf(
        ...Array.from({ length }, (_, i) =>
          timed(i, { task_id: `c${i}`, children: i + 1 < length ? [`c${i + 1}`] : [] }),
        ),
      ),
    }),
    tasks: (length) => length,
    containers: () => 0,
    last: (length) => `c${length - 1}`,
    depth: (length) => length - 1,
  },
  {
    // g0 holds m0 and the group g1, which holds m1 and g2, and so on
    name: 'groups nested in groups',
    build: (length) => ({
      root: 'g0',
      nodes: graphOf(
        ...Array.from({ length }, (_, i) => [
          node({
            task_id: `g${i}`,
            node_type: 'GROUP',
            group_id: `g${i}`,
            children: i + 1 < length ? [`m${i}`, `g${i + 1}`] : [`m${i}`],
          }),
          timed(i, { task_id: `m${i}`, group_id: `g${i}` }),
        ]).flat(),
      ),
    }),
    tasks: (length) => length,
    containers: (length) => length,
    last: (length) => `m${length - 1}`,
    depth: () => 0,
  },
  {
    // a task starts a group of one, whose member starts the next task
    name: 'a chain whose every step is a group',
    build: (length) => ({
      root: 't0',
      nodes: graphOf(
        ...Array.from({ length }, (_, i) => [
          timed(2 * i, { task_id: `t${i}`, children: [`g${i}`] }),
          node({ task_id: `g${i}`, node_type: 'GROUP', group_id: `g${i}`, children: [`m${i}`] }),
          timed(2 * i + 1, {
            task_id: `m${i}`,
            group_id: `g${i}`,
            children: i + 1 < length ? [`t${i + 1}`] : [],
          }),
        ]).flat(),
      ),
    }),
    tasks: (length) => 2 * length,
    containers: (length) => length,
    last: (length) => `m${length - 1}`,
    depth: (length) => 2 * length - 1,
  },
  {
    // a chord whose callback starts the next chord
    name: 'a long callback chain',
    build: (length) => ({
      root: 'c0',
      nodes: graphOf(
        ...Array.from({ length }, (_, i) => [
          node({
            task_id: `c${i}`,
            node_type: 'CHORD',
            group_id: `c${i}`,
            chord_id: `cb${i}`,
            children: [`m${i}`, `cb${i}`],
          }),
          timed(2 * i, { task_id: `m${i}`, group_id: `c${i}` }),
          timed(2 * i + 1, {
            task_id: `cb${i}`,
            children: i + 1 < length ? [`c${i + 1}`] : [],
          }),
        ]).flat(),
      ),
    }),
    tasks: (length) => 2 * length,
    containers: (length) => length,
    last: (length) => `cb${length - 1}`,
    depth: (length) => length - 1,
  },
  {
    // a group of 13 collapses to its first 3 members and the failed ones; member 5 starts a long
    // chain that ends in a failure, so the collapse has to look all the way down it
    name: 'a collapsed group whose hidden member starts a chain',
    build: (length) => ({
      root: 'g',
      nodes: graphOf(
        node({
          task_id: 'g',
          node_type: 'GROUP',
          group_id: 'g',
          children: Array.from({ length: 13 }, (_, i) => `m${i}`),
        }),
        ...Array.from({ length: 13 }, (_, i) =>
          timed(i, { task_id: `m${i}`, group_id: 'g', children: i === 5 ? ['c0'] : [] }),
        ),
        ...Array.from({ length }, (_, i) =>
          timed(100 + i, {
            task_id: `c${i}`,
            state: i + 1 === length ? 'FAILURE' : 'SUCCESS',
            children: i + 1 < length ? [`c${i + 1}`] : [],
          }),
        ),
      ),
    }),
    tasks: (length) => 4 + length,
    containers: () => 1,
    last: (length) => `c${length - 1}`,
    depth: (length) => length,
  },
  {
    // a member that is a chain of `length` tasks, only the last of which carries the group's id
    name: 'a header chain in one group member',
    build: (length) => ({
      root: 'g',
      nodes: graphOf(
        node({ task_id: 'g', node_type: 'GROUP', group_id: 'g', children: ['h0'] }),
        ...Array.from({ length }, (_, i) =>
          timed(i, {
            task_id: `h${i}`,
            group_id: i + 1 === length ? 'g' : null,
            children: i + 1 < length ? [`h${i + 1}`] : [],
          }),
        ),
      ),
    }),
    tasks: (length) => length,
    containers: () => 1,
    last: (length) => `h${length - 1}`,
    depth: (length) => length - 1,
  },
]

test.describe('flow code on deep flows', () => {
  test.setTimeout(120_000)

  for (const shape of SHAPES) {
    for (const length of LENGTHS) {
      test(`lays out and times ${shape.name} of ${length}`, () => {
        const { nodes, root } = shape.build(length)

        const layout = layoutFlow(nodes, root, new Set())
        expect(layout.tasks).toHaveLength(shape.tasks(length))
        expect(layout.containers).toHaveLength(shape.containers(length))
        expect(layout.edges.length).toBeGreaterThanOrEqual(length - 1)

        const timeline = buildTimeline(nodes, layout, root, T0 + 1_000_000_000)
        expect(timeline.rows).toHaveLength(
          layout.tasks.length + layout.more.filter((block) => block.kind === 'more').length,
        )
        expect(timeline.rows.find((row) => row.id === shape.last(length))?.depth).toBe(
          shape.depth(length),
        )

        const path = criticalPath(layout, timeline.ends, root)
        expect(path).toEqual(timeline.path)
        expect(path[0]).toBe(root)
        expect(path.at(-1)).toBe(shape.last(length))
        expect(path.length).toBeGreaterThanOrEqual(length)
      })
    }
  }
})
