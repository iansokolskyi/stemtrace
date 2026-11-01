import { expect, test } from '@playwright/test'

import {
  COLLAPSE_AT,
  type FlowLayout,
  layoutFlow,
  type PlacedTask,
} from '../src/components/flow/layoutFlow'
import { trackPath } from '../src/components/flow/trackPath'
import {
  allCaptures,
  capture,
  chain,
  type FlowGraph,
  graphOf,
  group,
  node,
} from './fixtures/flow-graphs'

/**
 * `layoutFlow` over the graphs the API returns for the captures in tests/data/celery_flows.json
 * (tests/fixtures/flow-graphs.json, written by tests/data/export_flow_graphs.py), and over small
 * synthetic graphs for the shapes the server never sends. No browser: the layout is pure.
 */

interface Captured extends FlowGraph {}

function layoutOf(name: string, expanded: string[] = []): { layout: FlowLayout } & Captured {
  const { root_id, nodes } = capture(name)
  return { layout: layoutFlow(nodes, root_id, new Set(expanded)), root_id, nodes }
}

/** The placed tasks, by task id. */
function byId(layout: FlowLayout): Map<string, PlacedTask> {
  return new Map(layout.tasks.map((task) => [task.id, task]))
}

function edgePairs(layout: FlowLayout): string[] {
  return layout.edges.map((edge) => `${edge.from}>${edge.to}`).sort()
}

test.describe('layoutFlow: chains', () => {
  for (const length of [1, 2, 3, 4]) {
    test(`lays a chain of ${length} out left to right, one edge per link`, () => {
      const layout = layoutFlow(chain(length), 'c0', new Set())

      expect(layout.tasks.map(({ id, x, y, w, h }) => ({ id, x, y, w, h }))).toEqual(
        Array.from({ length }, (_, i) => ({ id: `c${i}`, x: 16 + i * 220, y: 16, w: 196, h: 50 })),
      )
      expect(
        layout.edges.map(({ from, to, x1, y1, x2, y2 }) => ({ from, to, x1, y1, x2, y2 })),
      ).toEqual(
        Array.from({ length: length - 1 }, (_, i) => ({
          from: `c${i}`,
          to: `c${i + 1}`,
          x1: 16 + i * 220 + 196,
          y1: 41,
          x2: 16 + (i + 1) * 220,
          y2: 41,
        })),
      )
      expect(layout.containers).toEqual([])
      expect(layout.width).toBe(16 + length * 220 - 24 + 16)
      expect(layout.height).toBe(82)
    })
  }

  test('lays the captured chain out like the synthetic one', () => {
    const { layout } = layoutOf('chain')

    expect(layout.tasks.map((task) => task.x)).toEqual([16, 236, 456])
    expect(layout.edges).toHaveLength(2)
  })

  test('returns nothing for a root the payload does not hold', () => {
    const layout = layoutFlow(chain(2), 'nope', new Set())

    expect(layout.tasks).toEqual([])
    expect(layout.edges).toEqual([])
  })
})

test.describe('layoutFlow: groups and chords', () => {
  test('draws a group as a box with its members on parallel tracks and no edges', () => {
    const { layout, root_id } = layoutOf('group')

    expect(layout.containers).toHaveLength(1)
    const [box] = layout.containers
    expect(box).toMatchObject({
      id: root_id,
      type: 'GROUP',
      x: 16,
      y: 16,
      w: 230,
      h: 186,
      hub: 116,
      memberCount: 3,
      sharedName: 'add',
      succeeded: 3,
      failed: 0,
      other: 0,
      callbackId: null,
      collapsible: false,
    })
    expect(layout.tasks.map((task) => [task.x, task.y, task.w, task.h, task.containerId])).toEqual([
      [36, 60, 190, 32, root_id],
      [36, 100, 190, 32, root_id],
      [36, 140, 190, 32, root_id],
    ])
    expect(box.tracks.map((track) => track.y)).toEqual([76, 116, 156])
    expect(layout.edges).toEqual([])
  })

  test('draws a chord callback right of the box with one edge per member', () => {
    const { layout, nodes } = layoutOf('chord@field')
    const [box] = layout.containers
    const callback = byId(layout).get(box.callbackId as string) as PlacedTask

    expect(box).toMatchObject({ type: 'CHORD', x: 16, y: 16, w: 230, h: 186, hub: 116 })
    expect(callback).toMatchObject({ x: 270, y: 91, w: 196, h: 50, containerId: null })
    expect(nodes[box.callbackId as string].name).toMatch(/tsum$/)
    const members = layout.tasks.filter((task) => task.containerId === box.id)
    expect(members).toHaveLength(3)
    expect(layout.edges).toHaveLength(3)
    for (const [i, member] of members.entries()) {
      expect(layout.edges.find((edge) => edge.from === member.id)).toEqual({
        from: member.id,
        to: callback.id,
        x1: 226,
        y1: 76 + i * 40,
        x2: 270,
        y2: 116,
        failed: false,
        notRun: false,
        fan: false,
        junction: { x: 246, y: 116 },
      })
    }
    expect(box.tracks.every((track) => track.railOut)).toBe(true)
  })

  test('draws the callback of the old event shape outside, from the header that reported it', () => {
    // Before chord_id was sent the callback is only a child of the first header
    const { layout, nodes } = layoutOf('chord')
    const [box] = layout.containers
    const header = Object.values(nodes).find((entry) =>
      entry.children.some((id) => /tsum$/.test(nodes[id]?.name)),
    )

    expect(box.callbackId).toBeNull()
    expect(layout.edges).toHaveLength(1)
    expect(layout.edges[0].from).toBe(header?.task_id)
    expect(layout.edges[0].junction).toBeNull()
    expect(byId(layout).get(layout.edges[0].to)).toMatchObject({ x: 270, y: 16, containerId: null })
  })

  test('draws the tasks of a chord body that is a chain', () => {
    // chord([a, b], chain(tsum, add)): the callback has a child of its own, which the old
    // renderer dropped
    const { layout, nodes } = layoutOf('chord_with_chain_body@field')
    const [box] = layout.containers
    const callback = nodes[box.callbackId as string]
    const [bodyChild] = callback.children

    expect(byId(layout).get(bodyChild)).toMatchObject({ x: 490, containerId: null })
    expect(layout.edges.filter((edge) => edge.from === callback.task_id)).toEqual([
      expect.objectContaining({
        from: callback.task_id,
        to: bodyChild,
        x1: 466,
        y1: 96,
        x2: 490,
        y2: 96,
      }),
    ])
    expect(layout.tasks).toHaveLength(4)
  })

  test('draws each chain of a group of chains as a row of two cells inside the group', () => {
    const { layout, root_id } = layoutOf('group_of_chains')
    const [box] = layout.containers

    expect(box).toMatchObject({ id: root_id, w: 444, h: 146, memberCount: 2, hub: 96 })
    expect(layout.tasks.map((task) => [task.x, task.y])).toEqual([
      [36, 60],
      [250, 60],
      [36, 100],
      [250, 100],
    ])
    expect(layout.tasks.every((task) => task.containerId === root_id)).toBe(true)
    expect(layout.edges).toHaveLength(2)
    expect(layout.edges.map((edge) => [edge.x1, edge.y1, edge.x2, edge.y2])).toEqual([
      [226, 76, 250, 76],
      [226, 116, 250, 116],
    ])
  })

  test('draws a group a chord header starts once, as its own container', () => {
    const { layout, nodes } = layoutOf('chord_header_fanout_group@field')
    const inner = layout.containers.find((entry) => entry.type === 'GROUP')
    const outer = layout.containers.find((entry) => entry.type === 'CHORD')
    const starter = Object.values(nodes).find((entry) => entry.children.includes(inner?.id ?? ''))

    expect(layout.containers).toHaveLength(2)
    expect(inner).toMatchObject({ x: 270, y: 80, hub: 160, memberCount: 2 })
    expect(outer?.memberCount).toBe(2)
    expect(layout.tasks).toHaveLength(5)
    expect(layout.edges.filter((edge) => edge.to === inner?.id)).toEqual([
      expect.objectContaining({ from: starter?.task_id, x1: 226, y1: 76, x2: 270, y2: 160 }),
    ])
  })

  test('draws a chord header subtask outside the box, with an edge from its header', () => {
    const { layout, nodes } = layoutOf('chord_header_subtask@field')
    const [box] = layout.containers
    const header = Object.values(nodes).find(
      (entry) =>
        entry.node_type === 'TASK' && entry.children.some((id) => nodes[id].group_id === null),
    )
    const [subtask] = header?.children ?? []

    expect(byId(layout).get(subtask)).toMatchObject({ containerId: null, x: 270 })
    expect(
      layout.edges
        .filter((edge) => edge.from === header?.task_id)
        .map((edge) => edge.to)
        .sort(),
    ).toEqual([box.callbackId, subtask].sort())
    // each member to the callback, and the header to its subtask
    expect(layout.edges).toHaveLength(box.memberCount + 1)
  })

  test('draws a chord whose body is a chord as the outer callback container', () => {
    const { layout, nodes } = layoutOf('chord_in_chord_body@field')
    const outer = layout.containers.find(
      (entry) => entry.id === capture('chord_in_chord_body@field').root_id,
    )
    const inner = layout.containers.find((entry) => entry.id === outer?.callbackId)

    expect(layout.containers).toHaveLength(2)
    expect(outer).toMatchObject({ type: 'CHORD', x: 16 })
    expect(inner).toMatchObject({ type: 'CHORD', x: 270 })
    // two outer members to the inner container, two inner members to the inner callback
    expect(layout.edges.filter((edge) => edge.to === inner?.id)).toHaveLength(2)
    expect(layout.edges.filter((edge) => edge.to === inner?.callbackId)).toHaveLength(2)
    expect(byId(layout).get(inner?.callbackId as string)).toMatchObject({ x: 524 })
    expect(Object.keys(nodes)).toHaveLength(7)
  })
})

test.describe('layoutFlow: replace()', () => {
  test('draws the replacing task after the one that replaced itself', () => {
    const { layout, root_id } = layoutOf('replace_chain')

    expect(layout.tasks.map((task) => [task.id === root_id, task.x])).toEqual([
      [true, 16],
      [false, 236],
    ])
    expect(layout.edges).toHaveLength(1)
  })

  test('draws a replaced task that became a group as a container after it', () => {
    const { layout, root_id } = layoutOf('replace_group')

    expect(layout.containers).toHaveLength(1)
    expect(layout.containers[0]).toMatchObject({ x: 236, memberCount: 2 })
    expect(layout.edges).toEqual([
      expect.objectContaining({ from: root_id, to: layout.containers[0].id, x1: 212 }),
    ])
  })

  test('draws a replace that sits mid-graph once, with its replacement after it', () => {
    const { layout, nodes } = layoutOf('nested_replace_group@worker_process')
    const ids = [
      ...layout.tasks.map((task) => task.id),
      ...layout.containers.map((entry) => entry.id),
    ]

    expect(new Set(ids).size).toBe(ids.length)
    expect(ids).toHaveLength(Object.keys(nodes).length)
    expect(layout.containers[0].x).toBeGreaterThan(layout.tasks[0].x + 196)
  })
})

test.describe('layoutFlow: malformed links', () => {
  test('draws a group listed among its own children once', () => {
    const graph = group(2)
    graph.g.children = ['g', ...graph.g.children]

    const layout = layoutFlow(graph, 'g', new Set())

    expect(layout.containers).toHaveLength(1)
    expect(layout.tasks).toHaveLength(2)
    expect(layout.edges).toEqual([])
  })

  test('draws tasks whose children form a cycle once each', () => {
    const graph = graphOf(
      node({ task_id: 'a', children: ['b'] }),
      node({ task_id: 'b', children: ['a'] }),
    )

    const layout = layoutFlow(graph, 'a', new Set())

    expect(layout.tasks.map((task) => task.id)).toEqual(['a', 'b'])
    // the link back is an edge, drawn from b back to a
    expect(layout.edges.map((edge) => `${edge.from}>${edge.to}`).sort()).toEqual(['a>b', 'b>a'])
  })

  test('draws a chord callback once when its parent lists it first', () => {
    const chord = graphOf(
      node({ task_id: 'p', children: ['cb', 'ch'] }),
      node({
        task_id: 'ch',
        node_type: 'CHORD',
        group_id: 'g',
        chord_id: 'cb',
        children: ['h1', 'h2', 'cb'],
      }),
      node({ task_id: 'h1', group_id: 'g' }),
      node({ task_id: 'h2', group_id: 'g' }),
      node({ task_id: 'cb', name: 'app.total' }),
    )

    const layout = layoutFlow(chord, 'p', new Set())

    expect(layout.tasks.filter((task) => task.id === 'cb')).toHaveLength(1)
    // right of the chord, where its callback goes, not in the parent's column
    expect(byId(layout).get('cb')).toMatchObject({ x: 236 + 230 + 24, containerId: null })
    expect(layout.tasks).toHaveLength(4)
    // parent to callback, parent to chord, and one edge per header to the callback
    expect(edgePairs(layout)).toEqual(['h1>cb', 'h2>cb', 'p>cb', 'p>ch'])
  })

  test('skips a child the payload does not hold', () => {
    const graph = graphOf(node({ task_id: 'a', children: ['ghost', 'b'] }), node({ task_id: 'b' }))

    const layout = layoutFlow(graph, 'a', new Set())

    expect(layout.tasks.map((task) => task.id)).toEqual(['a', 'b'])
    expect(edgePairs(layout)).toEqual(['a>b'])
  })

  test('draws a container listed as a head of a container, outside it', () => {
    const graph = group(2, {}, 'CHORD')
    graph.m0.children = []
    graph.g.children.push('inner')
    graph.inner = node({ task_id: 'inner', node_type: 'GROUP', group_id: 'gi', children: ['x1'] })
    graph.x1 = node({ task_id: 'x1', group_id: 'gi' })

    const layout = layoutFlow(graph, 'g', new Set())

    expect(layout.containers.map((entry) => entry.id).sort()).toEqual(['g', 'inner'])
    expect(layout.edges).toEqual([
      expect.objectContaining({ from: 'g', to: 'inner', x1: 246, y1: 96 }),
    ])
  })
})

test.describe('layoutFlow: a callback that did not run', () => {
  const cases = [
    ['chord_failed_header@field', 'Chord header failed: 1 of 2 tasks failed'],
    ['chord_rejected_header', 'Chord header failed: 1 of 2 tasks failed'],
    ['chord_ignored_header', 'Chord header ignored'],
  ] as const

  for (const [name, text] of cases) {
    test(`draws a not-run box for ${name}`, () => {
      const { layout, root_id, nodes } = layoutOf(name)
      const chord = Object.values(nodes).find((entry) => entry.node_type === 'CHORD')

      expect(chord?.not_run_reason).toBeTruthy()
      expect(layout.notRun).toHaveLength(1)
      expect(layout.notRun[0]).toMatchObject({
        id: `${root_id}:not-run`,
        chordId: root_id,
        reason: chord?.not_run_reason,
        text,
        x: 270,
        w: 190,
        h: 80,
      })
      // centred on the chord's hub, joined to it by one dotted edge
      expect(layout.notRun[0].y + 40).toBe(layout.containers[0].hub)
      expect(layout.edges).toEqual([
        expect.objectContaining({
          from: root_id,
          to: `${root_id}:not-run`,
          notRun: true,
          junction: null,
        }),
      ])
    })
  }

  test('draws no not-run box once the chord has a callback', () => {
    const { layout } = layoutOf('chord@field')

    expect(layout.notRun).toEqual([])
  })

  test('says how many of the members failed', () => {
    const graph = group(5, { 1: 'FAILURE', 3: 'FAILURE' }, 'CHORD')
    graph.g.not_run_reason = 'header_failed'

    const layout = layoutFlow(graph, 'g', new Set())

    expect(layout.notRun[0].text).toBe('Chord header failed: 2 of 5 tasks failed')
  })

  test('says revoked and ignored without counts', () => {
    const revoked = group(2, { 0: 'REVOKED' }, 'CHORD')
    revoked.g.not_run_reason = 'header_revoked'

    expect(layoutFlow(revoked, 'g', new Set()).notRun[0].text).toBe('Chord header revoked')
  })
})

test.describe('layoutFlow: many members', () => {
  const shownIds = (layout: FlowLayout): string[] => layout.tasks.map((task) => task.id)

  test(`shows all ${COLLAPSE_AT} members of a container without a more block`, () => {
    const layout = layoutFlow(group(12), 'g', new Set())

    expect(shownIds(layout)).toHaveLength(12)
    expect(layout.more).toEqual([])
    expect(layout.containers[0]).toMatchObject({ memberCount: 12, collapsible: false })
  })

  test(`collapses ${COLLAPSE_AT + 1} members to the first three and a more block`, () => {
    const layout = layoutFlow(group(13), 'g', new Set())

    expect(shownIds(layout)).toEqual(['m0', 'm1', 'm2'])
    expect(layout.more).toEqual([
      expect.objectContaining({
        kind: 'more',
        hiddenCount: 10,
        summary: 'all succeeded',
        x: 36,
        y: 180,
      }),
    ])
    expect(layout.containers[0]).toMatchObject({
      memberCount: 13,
      succeeded: 13,
      collapsible: true,
      expanded: false,
      h: 44 + 4 * 32 + 3 * 8 + 30,
    })
  })

  test('keeps a failed member beyond the first three, in order', () => {
    const layout = layoutFlow(group(13, { 7: 'FAILURE', 11: 'FAILURE' }), 'g', new Set())

    expect(shownIds(layout)).toEqual(['m0', 'm1', 'm2', 'm7', 'm11'])
    expect(layout.more[0]).toMatchObject({ hiddenCount: 8, summary: 'all succeeded' })
    expect(layout.containers[0]).toMatchObject({ failed: 2, succeeded: 11, memberCount: 13 })
  })

  test('keeps a rejected member and one whose own subtask failed', () => {
    const graph = group(14, { 5: 'REJECTED' })
    graph.m9.children = ['sub']
    graph.sub = node({ task_id: 'sub', state: 'FAILURE' })

    const layout = layoutFlow(graph, 'g', new Set())

    expect(shownIds(layout).filter((id) => id.startsWith('m'))).toEqual([
      'm0',
      'm1',
      'm2',
      'm5',
      'm9',
    ])
    // the hidden members' subtrees are not drawn, the shown member's is
    expect(shownIds(layout)).toContain('sub')
  })

  test('summarises hidden members that are not all successes', () => {
    const graph = group(15, { 6: 'STARTED', 8: 'STARTED', 10: 'REVOKED' })

    const layout = layoutFlow(graph, 'g', new Set())

    expect(layout.more[0]).toMatchObject({
      hiddenCount: 12,
      summary: '9 succeeded, 2 running, 1 revoked',
    })
  })

  test('expands to every member in four columns, with a collapse block', () => {
    const layout = layoutFlow(group(48, { 30: 'FAILURE' }), 'g', new Set(['g']))
    const [box] = layout.containers

    expect(shownIds(layout)).toHaveLength(48)
    expect(layout.more).toEqual([
      expect.objectContaining({ kind: 'less', x: 36, y: 44 + 16 + 12 * 40 }),
    ])
    expect(box).toMatchObject({ w: 4 * 190 + 3 * 8 + 40, expanded: true, collapsible: true })
    expect(layout.tasks.slice(0, 5).map((task) => [task.x, task.y])).toEqual([
      [36, 60],
      [234, 60],
      [432, 60],
      [630, 60],
      [36, 100],
    ])
    // four members share a track
    expect(box.tracks[0].ids).toHaveLength(4)
  })

  test('expands rows of chains into one column', () => {
    const graph = group(13)
    for (let i = 0; i < 13; i++) {
      graph[`h${i}`] = node({ task_id: `h${i}`, children: [`m${i}`] })
      graph[`m${i}`].group_id = 'g'
    }
    graph.g.children = Array.from({ length: 13 }, (_, i) => `h${i}`)
    for (let i = 0; i < 13; i++) graph[`h${i}`].group_id = null

    const layout = layoutFlow(graph, 'g', new Set(['g']))

    expect(layout.containers[0].w).toBe(2 * 190 + 24 + 40)
    expect(
      new Set(layout.tasks.filter((task) => task.id.startsWith('h')).map((task) => task.x)),
    ).toEqual(new Set([36]))
  })

  test('collapses a chord of 48 with one failed header and shows its callback box', () => {
    const graph = group(48, { 40: 'FAILURE' }, 'CHORD')
    graph.g.not_run_reason = 'header_failed'

    const layout = layoutFlow(graph, 'g', new Set())

    expect(layout.tasks.map((task) => task.id)).toEqual(['m0', 'm1', 'm2', 'm40'])
    expect(layout.more[0]).toMatchObject({ hiddenCount: 44, summary: 'all succeeded' })
    expect(layout.notRun[0].text).toBe('Chord header failed: 1 of 48 tasks failed')
    expect(layout.containers[0]).toMatchObject({ succeeded: 47, failed: 1, other: 0 })
  })

  test('draws edges to the callback only from members that are shown', () => {
    const graph = group(20, {}, 'CHORD')
    graph.g.chord_id = 'cb'
    graph.g.children.push('cb')
    graph.cb = node({ task_id: 'cb', name: 'app.total' })

    const layout = layoutFlow(graph, 'g', new Set())

    expect(edgePairs(layout)).toEqual(['m0>cb', 'm1>cb', 'm2>cb'])
  })
})

test.describe('layoutFlow: every capture', () => {
  for (const [name, { root_id, nodes }] of Object.entries(allCaptures())) {
    test(`lays ${name} out without overlaps or dangling edges`, () => {
      const layout = layoutFlow(nodes, root_id, new Set())
      const ids = [
        ...layout.tasks.map((task) => task.id),
        ...layout.containers.map((entry) => entry.id),
        ...layout.notRun.map((box) => box.id),
      ]
      expect(new Set(ids).size, 'each node is drawn once').toBe(ids.length)
      for (const edge of layout.edges) {
        expect(ids, `edge ${edge.from} > ${edge.to} ends on drawn nodes`).toContain(edge.from)
        expect(ids).toContain(edge.to)
      }

      // Boxes that stand alone never overlap; a member sits inside its container
      const standalone = [
        ...layout.tasks.filter((task) => task.containerId === null),
        ...layout.containers,
        ...layout.notRun,
      ]
      for (const [i, a] of standalone.entries()) {
        for (const b of standalone.slice(i + 1)) {
          const overlap = a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h
          expect(overlap, `${a.id} overlaps ${b.id}`).toBe(false)
        }
      }
      for (const task of layout.tasks) {
        if (task.containerId === null) continue
        const box = layout.containers.find((entry) => entry.id === task.containerId)
        expect(box).toBeDefined()
        if (!box) continue
        expect(task.x).toBeGreaterThanOrEqual(box.x)
        expect(task.x + task.w).toBeLessThanOrEqual(box.x + box.w)
        expect(task.y).toBeGreaterThanOrEqual(box.y)
        expect(task.y + task.h).toBeLessThanOrEqual(box.y + box.h)
      }
    })
  }
})

test.describe('trackPath', () => {
  test('runs level when the ends are at the same height', () => {
    expect(trackPath(10, 40, 90, 40)).toBe('M10,40 H90')
  })

  test('eases through an S-curve between two heights', () => {
    expect(trackPath(0, 0, 100, 40)).toBe('M0,0 H24 C53.1,0 55.2,40 76,40 H100')
  })

  test('starts where the curve begins from a junction', () => {
    expect(trackPath(0, 0, 100, 40, true)).toBe('M24,0 C53.1,0 55.2,40 76,40 H100')
  })

  test('runs right to left when the target is behind', () => {
    expect(trackPath(100, 0, 0, 40)).toBe('M100,0 H76 C46.9,0 44.8,40 24,40 H0')
  })
})
