import { expect, test } from '@playwright/test'

import type { GraphNode } from '../src/api/client'
import { type FlowLayout, layoutFlow } from '../src/components/flow/layoutFlow'
import {
  type Bar,
  barOf,
  buildTimeline,
  criticalPath,
  type Timeline,
} from '../src/components/flow/timeline'
import { allCaptures, graphOf, node } from './fixtures/flow-graphs'

/**
 * The Timeline's rules (the PR body spells them out): where each bar starts, ends and waits, how
 * the axis is chosen, which rows exist and which of them are critical. No browser: all of it is
 * pure. Times are milliseconds after T0; `timed` writes them the way the API does.
 */

const T0 = Date.UTC(2026, 0, 1)
const iso = (ms: number): string => new Date(T0 + ms).toISOString()

interface Timing {
  /** first_seen */
  f: number | null
  /** last_updated; defaults to f */
  u?: number | null
  /** queue_wait_ms */
  w?: number | null
  /** runtime_ms */
  r?: number | null
  /** total_runtime_ms; defaults to r */
  tr?: number | null
  /** eta */
  eta?: number | null
}

/** A node with the timing fields the API sends for `t`; `extra` says the rest. */
function timed(id: string, t: Timing, extra: Partial<GraphNode> = {}): GraphNode {
  const u = t.u === undefined ? t.f : t.u
  return node({
    task_id: id,
    name: `app.${id}`,
    first_seen: t.f === null ? null : iso(t.f),
    last_updated: u === null || u === undefined ? null : iso(u),
    duration_ms: t.f !== null && u !== null && u !== undefined && u !== t.f ? u - t.f : null,
    queue_wait_ms: t.w ?? null,
    runtime_ms: t.r ?? null,
    total_runtime_ms: t.tr === undefined ? (t.r ?? null) : t.tr,
    eta: t.eta === undefined || t.eta === null ? null : iso(t.eta),
    ...extra,
  })
}

const barFor = (n: GraphNode) => barOf(n)

test.describe('barOf: where a bar starts, waits and ends', () => {
  test('a finished task starts at its last run and ends at its last event', () => {
    const bar = barFor(timed('a', { f: 0, w: 100, r: 300, u: 400 }))

    expect(bar).toEqual({
      from: T0,
      scheduledTo: T0,
      leadTo: T0 + 100,
      runFrom: T0 + 100,
      end: T0 + 400,
      hasRun: true,
    })
  })

  test('the run starts runtime_ms before the end, on the worker clock, not at first_seen + wait', () => {
    // The sender's clock is 100 ms ahead of the worker's: the lead-in absorbs the difference
    const bar = barFor(timed('a', { f: 0, w: 100, r: 300, u: 500 }))

    expect(bar).toMatchObject({ leadTo: T0 + 200, runFrom: T0 + 200, end: T0 + 500 })
  })

  test('a run that would start before first_seen starts there (skew)', () => {
    const bar = barFor(timed('a', { f: 100, w: 0, r: 300, u: 250 }))

    expect(bar).toMatchObject({
      from: T0 + 100,
      leadTo: T0 + 100,
      runFrom: T0 + 100,
      end: T0 + 250,
    })
  })

  test('a last_updated before first_seen counts as first_seen (skew)', () => {
    const bar = barFor(timed('a', { f: 100, w: 0, r: 10, u: 50 }))

    expect(bar).toMatchObject({ from: T0 + 100, runFrom: T0 + 100, end: T0 + 100 })
  })

  test('a running task has an open end and starts at its first start', () => {
    const bar = barFor(timed('a', { f: 0, w: 100, u: 100 }, { state: 'STARTED' }))

    expect(bar).toEqual({
      from: T0,
      scheduledTo: T0,
      leadTo: T0 + 100,
      runFrom: T0 + 100,
      end: null,
      hasRun: true,
    })
  })

  test('a running task without a recorded send starts at first_seen', () => {
    const bar = barFor(timed('a', { f: 50, u: 50 }, { state: 'STARTED' }))

    expect(bar).toMatchObject({ from: T0 + 50, leadTo: T0 + 50, runFrom: T0 + 50, end: null })
  })

  for (const state of ['PENDING', 'RECEIVED']) {
    test(`a ${state} task is queued: a lead-in that is still open and no run`, () => {
      const bar = barFor(timed('a', { f: 0, u: 0 }, { state }))

      expect(bar).toEqual({
        from: T0,
        scheduledTo: T0,
        leadTo: T0,
        runFrom: T0,
        end: null,
        hasRun: false,
      })
    })
  }

  test('a task that ended without ever starting is a closed lead-in and no run', () => {
    const bar = barFor(timed('a', { f: 0, u: 700 }, { state: 'REVOKED' }))

    expect(bar).toEqual({
      from: T0,
      scheduledTo: T0,
      leadTo: T0 + 700,
      runFrom: T0 + 700,
      end: T0 + 700,
      hasRun: false,
    })
  })

  test('a failure without a runtime but with a start runs from its start to its last event', () => {
    const bar = barFor(timed('a', { f: 0, w: 120, u: 900 }, { state: 'FAILURE' }))

    expect(bar).toMatchObject({ leadTo: T0 + 120, runFrom: T0 + 120, end: T0 + 900, hasRun: true })
  })

  test('a retried task keeps its first start and last run apart', () => {
    // first run started at 100, the last 200 ms run at 1300; 700 ms of runs in all
    const bar = barFor(timed('a', { f: 0, w: 100, r: 200, tr: 700, u: 1500 }))

    expect(bar).toEqual({
      from: T0,
      scheduledTo: T0,
      leadTo: T0 + 100,
      runFrom: T0 + 1300,
      end: T0 + 1500,
      hasRun: true,
    })
  })

  test('a total runtime equal to the last run is not a retry', () => {
    const bar = barFor(timed('a', { f: 0, w: 100, r: 200, tr: 200, u: 1500 }))

    expect(bar).toMatchObject({ leadTo: T0 + 1300, runFrom: T0 + 1300 })
  })

  test('a retrying task ends at its last event and starts at its first start', () => {
    const bar = barFor(timed('a', { f: 0, w: 100, u: 800 }, { state: 'RETRY' }))

    expect(bar).toMatchObject({ leadTo: T0 + 100, runFrom: T0 + 100, end: T0 + 800, hasRun: true })
  })

  test('a countdown or ETA waits from the ETA: the time before it is scheduled, not queued', () => {
    // queue_wait_ms counts from the ETA (attribution.queue_wait_ms): the run starts at eta + wait
    const bar = barFor(timed('a', { f: 0, eta: 5000, w: 50, r: 100, u: 5150 }))

    expect(bar).toEqual({
      from: T0,
      scheduledTo: T0 + 5000,
      leadTo: T0 + 5050,
      runFrom: T0 + 5050,
      end: T0 + 5150,
      hasRun: true,
    })
  })

  test('an ETA before first_seen changes nothing', () => {
    const bar = barFor(timed('a', { f: 0, eta: -1000, w: 50, r: 100, u: 150 }))

    expect(bar).toMatchObject({ scheduledTo: T0, leadTo: T0 + 50, end: T0 + 150 })
  })

  test('an ETA after the run starts is cut at the start (skew)', () => {
    const bar = barFor(timed('a', { f: 0, eta: 9000, w: 0, r: 100, u: 200 }))

    expect(bar).toMatchObject({ scheduledTo: T0 + 100, leadTo: T0 + 100, runFrom: T0 + 100 })
  })

  test('a queued task with an ETA keeps the ETA as its scheduled end', () => {
    const bar = barFor(timed('a', { f: 0, eta: 60_000, u: 0 }, { state: 'PENDING' }))

    expect(bar).toMatchObject({ scheduledTo: T0 + 60_000, end: null, hasRun: false })
  })

  test('without first_seen there is no bar', () => {
    expect(barFor(timed('a', { f: null }))).toBeNull()
    expect(barFor(timed('a', { f: 0 }, { first_seen: 'not a time' }))).toBeNull()
  })

  test('duration_ms plays no part: it is last_updated - first_seen', () => {
    const base = timed('a', { f: 0, w: 100, r: 300, u: 400 })

    expect(barFor({ ...base, duration_ms: 123_456 })).toEqual(barFor(base))
    expect(barFor({ ...base, duration_ms: null })).toEqual(barFor(base))
  })

  test('a missing last_updated counts as first_seen', () => {
    const bar = barFor(timed('a', { f: 10, u: null, w: 0, r: 0 }))

    expect(bar).toMatchObject({ from: T0 + 10, runFrom: T0 + 10, end: T0 + 10 })
  })

  test('negative or non-numeric waits and runtimes count as unknown', () => {
    const bar = barFor(
      timed('a', { f: 0, u: 400 }, { queue_wait_ms: -5, runtime_ms: Number.NaN, state: 'FAILURE' }),
    )

    expect(bar).toMatchObject({ hasRun: false, end: T0 + 400 })
  })
})

// --- graphs ---------------------------------------------------------------------------------

/** a -> b -> c, each starting when the one before ended. */
function timedChain(): Record<string, GraphNode> {
  return graphOf(
    timed('a', { f: 0, w: 0, r: 1000, u: 1000 }, { children: ['b'] }),
    timed('b', { f: 1000, w: 200, r: 500, u: 1700 }, { children: ['c'], parent_id: 'a' }),
    timed('c', { f: 1700, w: 0, r: 300, u: 2000 }, { parent_id: 'b' }),
  )
}

/** root -> chord of `count` headers -> callback `cb`. `ends[i]` is when header i ends. */
function timedChord(
  count: number,
  ends: (i: number) => number,
  extra: (i: number) => Partial<GraphNode> = () => ({}),
): Record<string, GraphNode> {
  const members = Array.from({ length: count }, (_, i) =>
    timed(
      `m${i}`,
      { f: 1000, w: 0, r: ends(i) - 1000, u: ends(i) },
      { group_id: 'ch', ...extra(i) },
    ),
  )
  const latest = Math.max(
    1000,
    ...members.map((m) => Date.parse(m.last_updated ?? '') - T0).filter(Number.isFinite),
  )
  return graphOf(
    timed('root', { f: 0, w: 0, r: 1000, u: 1000 }, { children: ['ch'] }),
    node({
      task_id: 'ch',
      name: 'chord',
      node_type: 'CHORD',
      group_id: 'ch',
      chord_id: 'cb',
      children: [...members.map((m) => m.task_id), 'cb'],
      parent_id: 'root',
    }),
    ...members,
    timed('cb', { f: latest, w: 0, r: 100, u: latest + 100 }, { parent_id: 'ch' }),
  )
}

function timelineOf(
  nodes: Record<string, GraphNode>,
  rootId: string,
  expandAll = false,
  now = 0,
): { timeline: Timeline; layout: FlowLayout } {
  const containers = Object.values(nodes)
    .filter((entry) => entry.node_type !== 'TASK')
    .map((entry) => entry.task_id)
  const layout = layoutFlow(nodes, rootId, new Set(expandAll ? containers : []))
  return { timeline: buildTimeline(nodes, layout, rootId, now), layout }
}

const rowOf = (timeline: Timeline, id: string) => {
  const row = timeline.rows.find((entry) => entry.id === id)
  if (!row) throw new Error(`no row ${id}: ${timeline.rows.map((r) => r.id).join(',')}`)
  return row
}
const barIn = (timeline: Timeline, id: string): Bar => {
  const bar = rowOf(timeline, id).bar
  if (!bar) throw new Error(`no bar on ${id}`)
  return bar
}

test.describe('buildTimeline: the axis', () => {
  test('starts at the earliest first_seen and ends at the latest end', () => {
    const { timeline } = timelineOf(timedChain(), 'a')

    expect(timeline.axis).toMatchObject({ t0: T0, t1: T0 + 2000 })
  })

  test('is at least one second long', () => {
    const nodes = graphOf(timed('a', { f: 0, w: 0, r: 40, u: 40 }))
    const { timeline } = timelineOf(nodes, 'a')

    expect(timeline.axis).toMatchObject({ t0: T0, t1: T0 + 1000 })
  })

  test('has a tick at every nice step from the origin, the first one labelled with the start', () => {
    const { timeline } = timelineOf(timedChain(), 'a')

    expect(timeline.axis?.ticks.map((tick) => tick.t - T0)).toEqual([0, 500, 1000, 1500, 2000])
    expect(timeline.axis?.ticks.map((tick) => tick.label)).toEqual([
      expect.stringMatching(/^start \d\d:\d\d:\d\d$/),
      '+500ms',
      '+1.0s',
      '+1.5s',
      '+2.0s',
    ])
  })

  test('has at most six ticks, none past the end', () => {
    for (const span of [1000, 1234, 7777, 60_000, 3_600_000, 86_400_000]) {
      const nodes = graphOf(timed('a', { f: 0, w: 0, r: span, u: span }))
      const ticks = timelineOf(nodes, 'a').timeline.axis?.ticks ?? []

      expect(ticks.length).toBeLessThanOrEqual(6)
      expect(ticks.length).toBeGreaterThanOrEqual(2)
      expect(Math.max(...ticks.map((tick) => tick.t))).toBeLessThanOrEqual(T0 + span)
    }
  })

  test('has no axis when no row has a time', () => {
    const { timeline } = timelineOf(graphOf(timed('a', { f: null })), 'a')

    expect(timeline.axis).toBeNull()
    expect(timeline.rows.map((row) => [row.id, row.bar])).toEqual([['a', null]])
  })

  test('an open bar runs to the axis end, which a later closed bar sets', () => {
    const nodes = graphOf(
      timed('a', { f: 0, w: 0, u: 0 }, { state: 'STARTED', children: ['b'] }),
      timed('b', { f: 500, w: 0, r: 1500, u: 2000 }),
    )
    const { timeline } = timelineOf(nodes, 'a')

    expect(barIn(timeline, 'a')).toMatchObject({ runFrom: T0, end: T0 + 2000, open: true })
    expect(barIn(timeline, 'b')).toMatchObject({ end: T0 + 2000, open: false })
  })

  test('a queued task is an open lead-in to the axis end', () => {
    const nodes = graphOf(
      timed('a', { f: 0, w: 0, r: 800, u: 800 }, { children: ['b'] }),
      timed('b', { f: 800, u: 800 }, { state: 'PENDING' }),
    )
    const { timeline } = timelineOf(nodes, 'a')

    expect(barIn(timeline, 'b')).toMatchObject({
      from: T0 + 800,
      leadTo: T0 + 1000,
      runFrom: T0 + 1000,
      end: T0 + 1000,
      hasRun: false,
      open: true,
    })
    expect(timeline.axis?.t1).toBe(T0 + 1000)
  })

  test('a scheduled end past the axis is cut at the axis end', () => {
    const nodes = graphOf(timed('a', { f: 0, eta: 3_600_000, u: 0 }, { state: 'PENDING' }))
    const { timeline } = timelineOf(nodes, 'a')

    expect(barIn(timeline, 'a')).toMatchObject({ scheduledTo: T0 + 1000, leadTo: T0 + 1000 })
  })
})

test.describe('buildTimeline: now', () => {
  const running = () => graphOf(timed('a', { f: 0, w: 100, u: 100 }, { state: 'STARTED' }))

  test('a running task runs from its start to now', () => {
    const { timeline } = timelineOf(running(), 'a', false, T0 + 5000)

    expect(barIn(timeline, 'a')).toMatchObject({
      runFrom: T0 + 100,
      end: T0 + 5000,
      open: true,
    })
    expect(timeline.axis?.t1).toBe(T0 + 5000)
  })

  test('the axis grows as now moves on', () => {
    const early = timelineOf(running(), 'a', false, T0 + 2000).timeline
    const late = timelineOf(running(), 'a', false, T0 + 9000).timeline

    expect([early.axis?.t1, late.axis?.t1]).toEqual([T0 + 2000, T0 + 9000])
  })

  test('a queued task waits until now', () => {
    const nodes = graphOf(timed('a', { f: 0, u: 0 }, { state: 'PENDING' }))

    expect(barIn(timelineOf(nodes, 'a', false, T0 + 4000).timeline, 'a')).toMatchObject({
      from: T0,
      end: T0 + 4000,
      hasRun: false,
    })
  })

  test('now is held at the latest event: a clock behind the server cannot shrink a bar', () => {
    const nodes = graphOf(
      timed('a', { f: 0, w: 0, u: 0 }, { state: 'STARTED', children: ['b'] }),
      timed('b', { f: 500, w: 0, r: 1500, u: 2000 }, { parent_id: 'a' }),
    )
    const { timeline } = timelineOf(nodes, 'a', false, T0 - 60_000)

    expect(barIn(timeline, 'a')).toMatchObject({ end: T0 + 2000 })
    expect(timeline.axis).toMatchObject({ t0: T0, t1: T0 + 2000 })
  })

  test('a flow with nothing open ignores now', () => {
    const { timeline } = timelineOf(timedChain(), 'a', false, T0 + 3_600_000)

    expect(timeline.axis?.t1).toBe(T0 + 2000)
  })
})

test.describe('buildTimeline: the rows', () => {
  test('are the diagram in layout order, one per task, none for a container', () => {
    const { timeline, layout } = timelineOf(
      timedChord(3, (i) => 2000 + i * 100),
      'root',
    )

    expect(timeline.rows.map((row) => row.id)).toEqual(['root', 'm0', 'm1', 'm2', 'cb'])
    expect(timeline.rows.map((row) => row.id)).toEqual(
      layout.tasks.map((task) => task.id), // seq order
    )
    expect(timeline.rows.every((row) => row.kind === 'task')).toBe(true)
  })

  test('indent by depth: members at the chord, the callback beside them, a child below', () => {
    const { timeline } = timelineOf(
      timedChord(2, () => 2000),
      'root',
    )

    expect(timeline.rows.map((row) => [row.id, row.depth])).toEqual([
      ['root', 0],
      ['m0', 1],
      ['m1', 1],
      ['cb', 1],
    ])
    expect(timelineOf(timedChain(), 'a').timeline.rows.map((row) => row.depth)).toEqual([0, 1, 2])
  })

  test('a collapsed container ends with a "+N more" row that selects nothing and expands', () => {
    const nodes = timedChord(13, (i) => 2000 + i * 10)
    const { timeline } = timelineOf(nodes, 'root')
    const more = timeline.rows.find((row) => row.kind === 'more')

    expect(timeline.rows.map((row) => row.id)).toEqual(['root', 'm0', 'm1', 'm2', 'ch', 'cb'])
    expect(more).toMatchObject({ id: 'ch', hidden: 10, depth: 1 })
  })

  test('the "+N more" bar spans from the first hidden start to the last hidden end', () => {
    // Rows run in first_seen order, so the hidden ones are the last ten to start: m3..m12
    const nodes = timedChord(
      13,
      (i) => 2000 + (i === 7 ? 600 : i * 10),
      (i) => ({ first_seen: iso(1000 + i * 10) }),
    )
    const { timeline } = timelineOf(nodes, 'root')
    const bar = timeline.rows.find((row) => row.kind === 'more')?.bar

    expect(bar).toMatchObject({
      from: T0 + 1030,
      leadTo: T0 + 1030,
      runFrom: T0 + 1030,
      end: T0 + 2600, // m7 ends last, a hidden task but not the last to start
      hasRun: true,
      open: false,
    })
  })

  test('the "+N more" bar is open when a hidden task is running', () => {
    const nodes = timedChord(
      13,
      (i) => 2000 + i * 10,
      (i) => (i === 5 ? { state: 'STARTED', runtime_ms: null, total_runtime_ms: null } : {}),
    )
    const { timeline } = timelineOf(nodes, 'root')
    const more = timeline.rows.find((row) => row.kind === 'more')

    expect(more?.bar).toMatchObject({ open: true, end: timeline.axis?.t1 })
  })

  test('expanding the container shows every member and no "+N more" row', () => {
    const { timeline } = timelineOf(
      timedChord(13, (i) => 2000 + i),
      'root',
      true,
    )

    expect(timeline.rows.filter((row) => row.kind === 'more')).toEqual([])
    expect(timeline.rows.map((row) => row.id)).toHaveLength(1 + 13 + 1)
  })

  test('a failed member stays a row beyond the third, as in the diagram', () => {
    const nodes = timedChord(
      13,
      (i) => 2000 + i,
      (i) => (i === 9 ? { state: 'FAILURE' } : {}),
    )
    const { timeline } = timelineOf(nodes, 'root')

    expect(timeline.rows.map((row) => row.id)).toContain('m9')
    expect(timeline.rows.find((row) => row.kind === 'more')).toMatchObject({ hidden: 9 })
  })

  test('a callback that did not run is a dotted row without a bar that selects its chord', () => {
    const members = Array.from({ length: 3 }, (_, i) =>
      timed(`m${i}`, { f: 1000, w: 0, r: 500, u: 1500 }, { group_id: 'ch' }),
    )
    const nodes = graphOf(
      timed('root', { f: 0, w: 0, r: 1000, u: 1000 }, { children: ['ch'] }),
      node({
        task_id: 'ch',
        name: 'chord',
        node_type: 'CHORD',
        state: 'FAILURE',
        group_id: 'ch',
        children: members.map((m) => m.task_id),
        parent_id: 'root',
        not_run_reason: 'header_failed',
      }),
      ...members,
    )
    const { timeline } = timelineOf(nodes, 'root')
    const notRun = timeline.rows.find((row) => row.kind === 'notRun')

    expect(notRun).toMatchObject({
      id: 'ch',
      centreId: 'ch:not-run',
      key: 'n:ch',
      bar: null,
      critical: false,
      depth: 1,
    })
    expect(timeline.rows.at(-1)).toBe(notRun)
  })

  test('a task that has no first_seen has a row and no bar', () => {
    const nodes = graphOf(
      timed('a', { f: 0, w: 0, r: 100, u: 100 }, { children: ['b'] }),
      timed('b', { f: null }, { parent_id: 'a' }),
    )
    const { timeline } = timelineOf(nodes, 'a')

    expect(rowOf(timeline, 'b').bar).toBeNull()
    expect(timeline.ends.has('b')).toBe(false)
  })
})

test.describe('criticalPath', () => {
  test('a chain is critical from end to end', () => {
    const { timeline } = timelineOf(timedChain(), 'a')

    expect(timeline.path).toEqual(['a', 'b', 'c'])
    expect(timeline.rows.map((row) => row.critical)).toEqual([true, true, true])
  })

  test('a single task is its own critical path', () => {
    const { timeline } = timelineOf(graphOf(timed('a', { f: 0, w: 0, r: 5, u: 5 })), 'a')

    expect(timeline.path).toEqual(['a'])
  })

  test('through a chord it takes the header that ends last, then the callback', () => {
    const nodes = timedChord(5, (i) => (i === 3 ? 4000 : 2000 + i))
    const { timeline } = timelineOf(nodes, 'root')

    expect(timeline.path).toEqual(['root', 'ch', 'm3', 'cb'])
    expect(timeline.rows.filter((row) => row.critical).map((row) => row.id)).toEqual([
      'root',
      'm3',
      'cb',
    ])
  })

  test('of equal ends the header the layout reached first wins', () => {
    const { timeline } = timelineOf(
      timedChord(4, () => 3000),
      'root',
    )

    expect(timeline.path).toEqual(['root', 'ch', 'm0', 'cb'])
  })

  test('without a callback it ends at the header that ends last', () => {
    const members = Array.from({ length: 3 }, (_, i) =>
      timed(`m${i}`, { f: 1000, w: 0, r: 500 + i * 100, u: 1500 + i * 100 }, { group_id: 'ch' }),
    )
    const nodes = graphOf(
      timed('root', { f: 0, w: 0, r: 1000, u: 1000 }, { children: ['ch'] }),
      node({
        task_id: 'ch',
        name: 'chord',
        node_type: 'CHORD',
        group_id: 'ch',
        children: members.map((m) => m.task_id),
        parent_id: 'root',
        not_run_reason: 'header_failed',
      }),
      ...members,
    )

    expect(timelineOf(nodes, 'root').timeline.path).toEqual(['root', 'ch', 'm2'])
  })

  test('a hidden header on the path puts the "+N more" row on it, and not a shown one', () => {
    const nodes = timedChord(13, (i) => (i === 8 ? 5000 : 2000 + i))
    const { timeline } = timelineOf(nodes, 'root')

    expect(timeline.path).toEqual(['root', 'ch', 'ch:more', 'cb'])
    expect(timeline.rows.filter((row) => row.critical).map((row) => row.key)).toEqual([
      't:root',
      'm:ch',
      't:cb',
    ])
  })

  test('the same header is on the path as itself once the container is expanded', () => {
    const nodes = timedChord(13, (i) => (i === 8 ? 5000 : 2000 + i))
    const { timeline } = timelineOf(nodes, 'root', true)

    expect(timeline.path).toEqual(['root', 'ch', 'm8', 'cb'])
  })

  test('a running task is on the path: it ends at the axis end', () => {
    const nodes = graphOf(
      timed('a', { f: 0, w: 0, r: 1000, u: 1000 }, { children: ['b', 'c'] }),
      timed('b', { f: 1000, w: 0, r: 3000, u: 4000 }, { parent_id: 'a' }),
      timed('c', { f: 1000, w: 0, u: 1000 }, { parent_id: 'a', state: 'STARTED' }),
    )
    const { timeline } = timelineOf(nodes, 'a')

    expect(timeline.path).toEqual(['a', 'c'])
  })

  test('a queued task, and one with no times, are never on it', () => {
    const nodes = graphOf(
      timed('a', { f: 0, w: 0, r: 1000, u: 1000 }, { children: ['b', 'c', 'd'] }),
      timed('b', { f: 1000, u: 1000 }, { parent_id: 'a', state: 'PENDING' }),
      timed('c', { f: null }, { parent_id: 'a' }),
      timed('d', { f: 1000, w: 0, r: 100, u: 1100 }, { parent_id: 'a' }),
    )

    expect(timelineOf(nodes, 'a').timeline.path).toEqual(['a', 'd'])
  })

  test('passes a task with no times to reach the ones after it', () => {
    const nodes = graphOf(
      timed('a', { f: 0, w: 0, r: 100, u: 100 }, { children: ['b'] }),
      timed('b', { f: null }, { parent_id: 'a', children: ['c'] }),
      timed('c', { f: 100, w: 0, r: 100, u: 200 }, { parent_id: 'b' }),
    )

    expect(timelineOf(nodes, 'a').timeline.path).toEqual(['a', 'b', 'c'])
  })

  test('is not cut short when a task ends a millisecond before the one it followed', () => {
    // b ends at 999, a's last event is at 1000: a stamping race or a worker clock 1 ms behind
    const nodes = graphOf(
      timed('a', { f: 0, w: 0, r: 1000, u: 1000 }, { children: ['b'] }),
      timed('b', { f: 900, w: 0, r: 99, u: 999 }, { parent_id: 'a', children: ['c'] }),
      timed('c', { f: 1000, w: 0, r: 5000, u: 6000 }, { parent_id: 'b' }),
    )

    expect(timelineOf(nodes, 'a').timeline.path).toEqual(['a', 'b', 'c'])
  })

  test('takes the member that ended last into a callback, whatever the skew', () => {
    const nodes = timedChord(3, (i) => (i === 1 ? 2999 : 3000))
    nodes.cb = { ...nodes.cb, ...timed('cb', { f: 2990, w: 0, r: 100, u: 3090 }) }

    expect(timelineOf(nodes, 'root').timeline.path).toEqual(['root', 'ch', 'm0', 'cb'])
  })

  test('ends at the latest end among the tasks it can reach, not a task that merely follows the root', () => {
    const nodes = graphOf(
      timed('a', { f: 0, w: 0, r: 1000, u: 1000 }, { children: ['b', 'c'] }),
      timed('b', { f: 900, w: 0, r: 50, u: 950 }, { parent_id: 'a' }),
      timed('c', { f: 1000, w: 0, r: 300, u: 1300 }, { parent_id: 'a' }),
    )

    expect(timelineOf(nodes, 'a').timeline.path).toEqual(['a', 'c'])
  })

  test('of two branches that end together the one the layout reached first is critical', () => {
    const nodes = graphOf(
      timed('a', { f: 0, w: 0, r: 100, u: 100 }, { children: ['b', 'c'] }),
      timed('b', { f: 100, w: 0, r: 400, u: 500 }, { parent_id: 'a' }),
      timed('c', { f: 100, w: 0, r: 400, u: 500 }, { parent_id: 'a' }),
    )

    expect(timelineOf(nodes, 'a').timeline.path).toEqual(['a', 'b'])
  })

  test('stops at a task whose children all ended before it did (they ran alongside it)', () => {
    // a starts b at once and runs on for 5 s; b is done at 600
    const nodes = graphOf(
      timed('a', { f: 0, w: 0, r: 5000, u: 5000 }, { children: ['b'] }),
      timed('b', { f: 100, w: 0, r: 500, u: 600 }, { parent_id: 'a' }),
    )

    expect(timelineOf(nodes, 'a').timeline.path).toEqual(['a'])
  })

  test('passes a chord when its header ends before the task that started it', () => {
    const nodes = timedChord(2, () => 1500)
    nodes.root = { ...nodes.root, ...timed('root', { f: 0, w: 0, r: 9000, u: 9000 }) }

    expect(timelineOf(nodes, 'root').timeline.path).toEqual(['root'])
  })

  test('a chord at the root starts the path at the chord', () => {
    const members = Array.from({ length: 2 }, (_, i) =>
      timed(`m${i}`, { f: 0, w: 0, r: 100 * (i + 1), u: 100 * (i + 1) }, { group_id: 'g' }),
    )
    const nodes = graphOf(
      node({
        task_id: 'g',
        name: 'group',
        node_type: 'GROUP',
        group_id: 'g',
        children: members.map((m) => m.task_id),
      }),
      ...members,
    )

    expect(timelineOf(nodes, 'g').timeline.path).toEqual(['g', 'm1'])
  })

  test('nothing timed gives an empty path', () => {
    const { timeline } = timelineOf(graphOf(timed('a', { f: null })), 'a')

    expect(timeline.path).toEqual([])
  })

  test('survives a cycle in the layout edges', () => {
    const nodes = graphOf(
      timed('a', { f: 0, w: 0, r: 100, u: 100 }, { children: ['b'] }),
      timed('b', { f: 100, w: 0, r: 100, u: 200 }, { children: ['a'], parent_id: 'a' }),
    )

    expect(timelineOf(nodes, 'a').timeline.path).toEqual(['a', 'b'])
  })

  test('criticalPath takes the ends it is given, not the bars', () => {
    const { layout } = timelineOf(timedChain(), 'a')

    expect(
      criticalPath(
        layout,
        new Map([
          ['a', 10],
          ['b', 20],
        ]),
        'a',
      ),
    ).toEqual(['a', 'b'])
  })
})

// --- sweeps ---------------------------------------------------------------------------------

/** Whether the path may step from `a` to `b`: a layout edge, or the container's own members. */
function linked(layout: FlowLayout, a: string, b: string): boolean {
  if (layout.edges.some((edge) => edge.from === a && edge.to === b)) return true
  const container = layout.containers.find((box) => box.id === a)
  if (container) {
    if (b === `${a}:more`) return layout.more.some((block) => block.containerId === a)
    return layout.tasks.some((task) => task.id === b && task.containerId === a)
  }
  if (a.endsWith(':more')) {
    const owner = layout.containers.find((box) => `${box.id}:more` === a)
    return owner?.callbackId === b
  }
  return false
}

/** Every id the root reaches by `linked` steps (written here, not read from the model). */
function reachable(layout: FlowLayout, rootId: string): Set<string> {
  const ids = [
    ...layout.tasks.map((task) => task.id),
    ...layout.containers.map((box) => box.id),
    ...layout.more.filter((block) => block.kind === 'more').map((b) => `${b.containerId}:more`),
  ]
  const seen = new Set([rootId])
  const queue = [rootId]
  while (queue.length > 0) {
    const from = queue.shift() as string
    for (const id of ids) {
      if (!seen.has(id) && linked(layout, from, id)) {
        seen.add(id)
        queue.push(id)
      }
    }
  }
  return seen
}

/** What is wrong with the timeline of `nodes`, as one line each; empty when every rule holds. */
function problemsWith(label: string, nodes: Record<string, GraphNode>, rootId: string): string[] {
  const problems: string[] = []
  for (const expandAll of [false, true]) {
    const { timeline, layout } = timelineOf(nodes, rootId, expandAll)
    const where = `${label}${expandAll ? ' (expanded)' : ''}`
    const check = (ok: boolean, what: string): void => {
      if (!ok) problems.push(`${where}: ${what}`)
    }
    const axis = timeline.axis
    const bars = timeline.rows.flatMap((row) => (row.bar ? [row.bar] : []))

    if (!axis) {
      check(bars.length === 0, 'bars without an axis')
      check(timeline.path.length === 0, 'a path without an axis')
      continue
    }
    check(Number.isFinite(axis.t0) && Number.isFinite(axis.t1), 'axis is not finite')
    check(axis.t1 - axis.t0 >= 1000, 'axis is shorter than one second')
    for (const tick of axis.ticks) {
      check(Number.isFinite(tick.t) && !tick.label.includes('NaN'), `tick ${tick.label}`)
      check(tick.t >= axis.t0 && tick.t <= axis.t1, `tick ${tick.label} is off the axis`)
    }
    for (const bar of bars) {
      const values = [bar.from, bar.scheduledTo, bar.leadTo, bar.runFrom, bar.end]
      check(values.every(Number.isFinite), `a bar is not finite: ${JSON.stringify(bar)}`)
      check(bar.from >= axis.t0, `a bar starts before the axis: ${JSON.stringify(bar)}`)
      check(bar.end <= axis.t1, `a bar ends after the axis: ${JSON.stringify(bar)}`)
      check(
        bar.from <= bar.scheduledTo &&
          bar.scheduledTo <= bar.leadTo &&
          bar.leadTo <= bar.runFrom &&
          bar.runFrom <= bar.end,
        `a bar is out of order: ${JSON.stringify(bar)}`,
      )
    }
    check(new Set(timeline.rows.map((row) => row.key)).size === timeline.rows.length, 'row keys')
    check(
      timeline.rows.every((row) => Number.isFinite(row.depth) && row.depth >= 0),
      'a depth is not a number',
    )

    // The critical path: a chain from the root, by layout edges, that never ends earlier
    const { path, ends } = timeline
    if (path.length === 0) continue
    check(path[0] === rootId, `path starts at ${path[0]}, not the root`)
    for (let i = 1; i < path.length; i++) {
      check(linked(layout, path[i - 1], path[i]), `${path[i - 1]} -> ${path[i]} is not a link`)
    }
    check(ends.has(path[path.length - 1]), 'the path ends on an untimed node')
    // Oracle: the path ends at the latest end among the timed nodes the root can reach
    const reach = reachable(layout, rootId)
    const latest = Math.max(...[...ends].filter(([id]) => reach.has(id)).map(([, end]) => end))
    check(
      ends.get(path[path.length - 1]) === latest,
      `path ends at ${ends.get(path[path.length - 1])}, the latest reachable end is ${latest}`,
    )
    const marked = timeline.rows.filter((row) => row.critical).length
    check(
      marked === path.filter((id) => !layout.containers.some((c) => c.id === id)).length,
      'rows marked critical are not the path',
    )
  }
  return problems
}

test.describe('timeline sweeps', () => {
  test('hold for every captured graph', () => {
    const captures = allCaptures()
    expect(Object.keys(captures)).toHaveLength(110)
    const problems = Object.entries(captures).flatMap(([name, { root_id, nodes }]) =>
      problemsWith(name, nodes, root_id),
    )
    expect(problems).toEqual([])
  })

  test('hold for every timing case in every position of a chain and a chord', () => {
    const cases: Array<[string, Timing, Partial<GraphNode>]> = [
      ['finished', { f: 0, w: 100, r: 300, u: 400 }, {}],
      ['finished, skewed', { f: 0, w: 100, r: 300, u: 500 }, {}],
      ['start before first_seen', { f: 100, w: 0, r: 300, u: 250 }, {}],
      ['end before first_seen', { f: 100, w: 0, r: 10, u: 50 }, {}],
      ['running', { f: 0, w: 100, u: 100 }, { state: 'STARTED' }],
      ['running, no wait', { f: 0, u: 0 }, { state: 'STARTED' }],
      ['queued', { f: 0, u: 0 }, { state: 'PENDING' }],
      ['received', { f: 0, u: 0 }, { state: 'RECEIVED' }],
      ['queued with ETA', { f: 0, eta: 9000, u: 0 }, { state: 'PENDING' }],
      ['revoked unstarted', { f: 0, u: 700 }, { state: 'REVOKED' }],
      ['failed, no runtime', { f: 0, w: 50, u: 900 }, { state: 'FAILURE' }],
      ['failed unstarted', { f: 0, u: 900 }, { state: 'FAILURE' }],
      ['retried', { f: 0, w: 100, r: 200, tr: 700, u: 1500 }, {}],
      ['retrying', { f: 0, w: 100, u: 800 }, { state: 'RETRY' }],
      ['ETA', { f: 0, eta: 5000, w: 50, r: 100, u: 5150 }, {}],
      ['ETA after the end', { f: 0, eta: 99_000, w: 0, r: 100, u: 200 }, {}],
      ['ETA before first_seen', { f: 0, eta: -5000, w: 0, r: 100, u: 200 }, {}],
      ['no first_seen', { f: null }, {}],
      ['no last_updated', { f: 0, u: null, w: 0, r: 5 }, {}],
      ['no duration', { f: 0, w: 0, r: 5, u: 0 }, { duration_ms: null }],
    ]
    const problems: string[] = []
    for (const [name, timing, extra] of cases) {
      const chainOf = graphOf(
        timed('a', { f: 0, w: 0, r: 100, u: 100 }, { children: ['b'] }),
        timed(
          'b',
          {
            ...timing,
            f: timing.f === null ? null : timing.f + 100,
            u: timing.u == null ? timing.u : timing.u + 100,
          },
          { ...extra, parent_id: 'a', children: ['c'] },
        ),
        timed('c', { f: 5000, w: 0, r: 100, u: 5100 }, { parent_id: 'b' }),
      )
      problems.push(...problemsWith(`chain, ${name}`, chainOf, 'a'))
      for (const count of [3, 13]) {
        const chord = timedChord(
          count,
          (i) => 2000 + i * 7,
          (i) =>
            i === 1 || i === count - 1
              ? {
                  ...timed(`m${i}`, timing, { group_id: 'ch' }),
                  ...extra,
                  group_id: 'ch',
                  task_id: `m${i}`,
                }
              : {},
        )
        problems.push(...problemsWith(`chord of ${count}, ${name}`, chord, 'root'))
      }
    }
    expect(problems).toEqual([])
  })

  test('hold when one task of a captured graph is shifted by clock skew', () => {
    test.setTimeout(180_000)
    const problems: string[] = []
    for (const [name, { root_id, nodes }] of Object.entries(allCaptures())) {
      for (const id of Object.keys(nodes)) {
        if (nodes[id].node_type !== 'TASK' || !nodes[id].first_seen) continue
        for (const skew of [-50, -5, -2, -1, 1, 2, 5, 50]) {
          const shift = (iso: string | null) =>
            iso === null ? null : new Date(Date.parse(iso) + skew).toISOString()
          const shifted = {
            ...nodes,
            [id]: {
              ...nodes[id],
              first_seen: shift(nodes[id].first_seen),
              last_updated: shift(nodes[id].last_updated),
            },
          }
          problems.push(...problemsWith(`${name}, ${id.slice(0, 6)} ${skew} ms`, shifted, root_id))
        }
      }
      if (problems.length > 20) break
    }
    expect(problems.slice(0, 20)).toEqual([])
  })

  test('hold for 400 seeded random timings of a chain, a chord and a group of chains', () => {
    // mulberry32: the same sequence every run
    let seed = 20_261_101
    const random = (): number => {
      seed = (seed + 0x6d2b79f5) | 0
      let t = Math.imul(seed ^ (seed >>> 15), 1 | seed)
      t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296
    }
    const pick = <T>(list: T[]): T => list[Math.floor(random() * list.length)]
    const maybe = (value: number): number | null => (random() < 0.2 ? null : value)
    const randomNode = (id: string, extra: Partial<GraphNode>): GraphNode => {
      const f = random() < 0.05 ? null : Math.floor(random() * 3000) - 500
      const r = maybe(Math.floor(random() * 2000))
      const tr = r === null ? null : r + (random() < 0.3 ? Math.floor(random() * 2000) : 0)
      return timed(
        id,
        {
          f,
          u: f === null ? null : f + Math.floor(random() * 4000) - 800,
          w: maybe(Math.floor(random() * 1500)),
          r,
          tr,
          eta: random() < 0.2 ? Math.floor(random() * 6000) - 1000 : null,
        },
        {
          state: pick([
            'SUCCESS',
            'FAILURE',
            'STARTED',
            'PENDING',
            'RECEIVED',
            'RETRY',
            'REVOKED',
            'IGNORED',
            'REJECTED',
          ]),
          ...extra,
        },
      )
    }
    const problems: string[] = []
    for (let round = 0; round < 400; round++) {
      const length = 1 + (round % 4)
      const chainNodes = graphOf(
        ...Array.from({ length }, (_, i) =>
          randomNode(`c${i}`, { children: i + 1 < length ? [`c${i + 1}`] : [] }),
        ),
      )
      problems.push(...problemsWith(`random chain ${round}`, chainNodes, 'c0'))

      const count = pick([2, 5, 12, 13, 20])
      const members = Array.from({ length: count }, (_, i) =>
        randomNode(`m${i}`, { group_id: 'ch' }),
      )
      const withCallback = random() < 0.7
      const chordNodes = graphOf(
        randomNode('root', { children: ['ch'] }),
        node({
          task_id: 'ch',
          name: 'chord',
          node_type: 'CHORD',
          group_id: 'ch',
          chord_id: withCallback ? 'cb' : null,
          not_run_reason: withCallback ? null : 'header_failed',
          children: [...members.map((m) => m.task_id), ...(withCallback ? ['cb'] : [])],
          parent_id: 'root',
        }),
        ...members,
        ...(withCallback ? [randomNode('cb', { parent_id: 'ch' })] : []),
      )
      problems.push(...problemsWith(`random chord ${round}`, chordNodes, 'root'))

      const chains = Array.from({ length: pick([2, 13]) }, (_, i) => [
        randomNode(`h${i}`, { group_id: 'g', children: [`t${i}`] }),
        randomNode(`t${i}`, {}),
      ])
      const groupNodes = graphOf(
        node({
          task_id: 'g',
          name: 'group',
          node_type: 'GROUP',
          group_id: 'g',
          children: chains.map(([head]) => head.task_id),
        }),
        ...chains.flat(),
      )
      problems.push(...problemsWith(`random group of chains ${round}`, groupNodes, 'g'))
    }
    expect(problems.slice(0, 20)).toEqual([])
  })
})
