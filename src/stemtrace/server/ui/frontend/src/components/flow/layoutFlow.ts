/**
 * Pure layout of a flow's graph as a left-to-right track diagram (no React).
 *
 * Structure follows the rules of the old React Flow renderer (what is inside a container, which
 * edges exist, what is drawn once); placement follows the prototype: a task's children stack to
 * its right and the task is centred on them, a container's members run on parallel tracks.
 * The rules are written out in the PR that added this file.
 */
import type { GraphNode, NodeType } from '@/api/client'

/** A container with more members than this shows the first three, the failed ones and "+N more". */
export const COLLAPSE_AT = 12
/** Members kept visible, besides the failed ones, in a collapsed container. */
export const COLLAPSED_HEAD = 3

export const TASK_W = 196
export const TASK_H = 50
export const MEMBER_W = 190
export const MEMBER_H = 32
export const NOT_RUN_W = 190
export const NOT_RUN_H = 80
/** Gap between columns, and between the cells of a member row. */
export const GAP_X = 24
/** Gap between sibling subtrees. */
export const GAP_Y = 14
/** Gap between member tracks. */
export const TRACK_GAP = 8
export const PAD = 20
export const HEAD = 44
export const FOOT = 30
export const MARGIN = 16
/** Columns a container's members flow into once expanded (single-cell rows only). */
export const EXPANDED_COLUMNS = 4

export type NotRunReason = 'header_failed' | 'header_revoked' | 'header_ignored'

export interface PlacedTask {
  id: string
  x: number
  y: number
  w: number
  h: number
  /** The container the task is a member (or a cell of a member row) of. */
  containerId: string | null
  /** Draw order and tab order: the order the layout reached it. */
  seq: number
}

export interface PlacedTrack {
  /** Vertical centre of the track. */
  y: number
  /** Every task block on the track. */
  ids: string[]
  /** A task on the track failed. */
  failed: boolean
  /** The track's way out of the container is part of the member to callback edges. */
  railOut: boolean
}

export interface PlacedContainer {
  id: string
  type: Exclude<NodeType, 'TASK'>
  x: number
  y: number
  w: number
  h: number
  /** Vertical centre of the tracks: where the incoming track splits and the outgoing one rejoins. */
  hub: number
  memberCount: number
  /** The member task's last name segment when every member is the same task, else null. */
  sharedName: string | null
  succeeded: number
  failed: number
  /** Members that neither succeeded nor failed. */
  other: number
  tracks: PlacedTrack[]
  callbackId: string | null
  /** More members than `COLLAPSE_AT`, so the container can collapse and expand. */
  collapsible: boolean
  expanded: boolean
  seq: number
}

/** The "+N more" and "Collapse" blocks of a container with many members. */
export interface PlacedMore {
  containerId: string
  kind: 'more' | 'less'
  x: number
  y: number
  w: number
  h: number
  /** Hidden members (`more` only). */
  hiddenCount: number
  /** Every task of the hidden members, in order (`more` only): the Timeline's aggregate row. */
  hiddenIds: string[]
  /** "all succeeded", or "3 running, 1 revoked" (`more` only). */
  summary: string
  seq: number
}

/** The box a chord's callback would have, when it did not run. */
export interface PlacedNotRun {
  /** `<chord id>:not-run`: the box has no task of its own. */
  id: string
  chordId: string
  reason: NotRunReason
  /** "Chord header failed: 1 of 48 tasks failed". */
  text: string
  x: number
  y: number
  w: number
  h: number
  seq: number
}

export interface PlacedEdge {
  from: string
  to: string
  x1: number
  y1: number
  x2: number
  y2: number
  /** The target failed: the section into it is lit in the failure colour. */
  failed: boolean
  /** Leads to a callback that did not run: drawn dotted. */
  notRun: boolean
  /** The source has several targets, so only the curve (not the shared stem) is lit. */
  fan: boolean
  /** A member to callback edge: the member's track rejoins the container's right edge here first. */
  junction: { x: number; y: number } | null
}

export interface FlowLayout {
  width: number
  height: number
  tasks: PlacedTask[]
  containers: PlacedContainer[]
  more: PlacedMore[]
  notRun: PlacedNotRun[]
  edges: PlacedEdge[]
}

const EMPTY: FlowLayout = {
  width: 0,
  height: 0,
  tasks: [],
  containers: [],
  more: [],
  notRun: [],
  edges: [],
}

const FAILED = new Set(['FAILURE', 'REJECTED'])

/** FAILURE and REJECTED: the states the flows list counts as failed. */
export function isFailedState(state: string): boolean {
  return FAILED.has(state)
}

const STATE_WORDS: Record<string, string> = {
  SUCCESS: 'succeeded',
  FAILURE: 'failed',
  RETRY: 'retrying',
  STARTED: 'running',
  PENDING: 'pending',
  RECEIVED: 'received',
  REVOKED: 'revoked',
  REJECTED: 'rejected',
  IGNORED: 'ignored',
}

/** "all succeeded", or "47 succeeded, 1 failed": the states of a set of tasks in words. */
export function describeStates(states: readonly string[]): string {
  const counts = new Map<string, number>()
  for (const state of states) counts.set(state, (counts.get(state) ?? 0) + 1)
  if (counts.size === 1 && counts.has('SUCCESS')) return 'all succeeded'
  return [...counts]
    .sort((a, b) => b[1] - a[1])
    .map(([state, count]) => `${count} ${STATE_WORDS[state] ?? state.toLowerCase()}`)
    .join(', ')
}

/** Why a chord's callback did not run, in words. `failed` and `members` fill "M of N tasks failed". */
export function notRunText(reason: NotRunReason, failed: number, members: number): string {
  switch (reason) {
    case 'header_failed':
      return `Chord header failed: ${failed} of ${members} tasks failed`
    case 'header_revoked':
      return 'Chord header revoked'
    case 'header_ignored':
      return 'Chord header ignored'
  }
}

export function lastNameSegment(name: string): string {
  return name.slice(name.lastIndexOf('.') + 1)
}

const typeOf = (node: GraphNode | undefined): NodeType => node?.node_type ?? 'TASK'

interface Extent {
  /** Height of the subtree placed, from the y it was placed at. */
  h: number
}

interface PendingEdge {
  from: string
  to: string
  x1: number
  y1: number
  fan: boolean
  junction: { x: number; y: number } | null
}

/**
 * Lays out the graph under `rootId`. `expanded` holds the containers whose members are all shown.
 * Nothing is placed for a missing root. Each node is placed once, however it is linked.
 */
export function layoutFlow(
  nodes: Record<string, GraphNode>,
  rootId: string,
  expanded: ReadonlySet<string>,
): FlowLayout {
  if (!nodes[rootId]) return EMPTY

  const isContainer = (id: string): boolean => typeOf(nodes[id]) !== 'TASK'

  /** Children that exist, without links back to the node itself (a malformed payload has them). */
  const childrenOf = (id: string): string[] =>
    (nodes[id]?.children ?? []).filter((child) => child !== id && nodes[child] !== undefined)

  const startOf = (id: string): number => {
    const time = Date.parse(nodes[id]?.first_seen ?? '')
    return Number.isNaN(time) ? Number.POSITIVE_INFINITY : time
  }
  /** By first_seen, the server's order for ties and missing times. */
  const byStart = (ids: string[]): string[] =>
    ids
      .map((id, index) => ({ id, index, start: startOf(id) }))
      .sort((a, b) => (a.start === b.start ? a.index - b.index : a.start < b.start ? -1 : 1))
      .map((entry) => entry.id)

  // A chord's callback is the child its chord_id names
  const callbackOf = new Map<string, string>()
  for (const [id, node] of Object.entries(nodes)) {
    const callback = node.chord_id
    if (
      typeOf(node) === 'CHORD' &&
      callback &&
      callback !== id &&
      nodes[callback] &&
      node.children.includes(callback)
    ) {
      callbackOf.set(id, callback)
    }
  }
  const callbacks = new Set(callbackOf.values())

  /** A member: the head child, then single TASK children until the group's own member. */
  const rowFrom = (headId: string, groupId: string | null): string[] => {
    const row = [headId]
    let current = nodes[headId]
    while (current && current.group_id !== groupId) {
      const next = childrenOf(row[row.length - 1])
      const nextId = next.length === 1 ? next[0] : undefined
      // Only tasks continue a row: a group or chord child is drawn as its own container
      if (nextId === undefined || typeOf(nodes[nextId]) !== 'TASK' || row.includes(nextId)) break
      row.push(nextId)
      current = nodes[nextId]
    }
    return row
  }

  /** The rows a container draws, and the container children that are not rows. */
  const partsOf = (id: string): { rows: string[][]; containerHeads: string[] } => {
    const heads = childrenOf(id).filter((child) => child !== callbackOf.get(id))
    const taskHeads = byStart(heads.filter((head) => !isContainer(head)))
    return {
      rows: taskHeads.map((head) => rowFrom(head, nodes[id].group_id)),
      containerHeads: byStart(heads.filter(isContainer)),
    }
  }

  // Cells of rows belong to their container; the root is always drawn on its own
  const cells = new Set<string>()
  for (const [id, node] of Object.entries(nodes)) {
    if (typeOf(node) === 'TASK') continue
    for (const row of partsOf(id).rows) for (const cell of row) cells.add(cell)
  }
  cells.delete(rootId)

  const failedDescendants = new Map<string, boolean>()
  /** The node or something it started failed (a hidden member must not hide that). */
  const hasFailure = (id: string): boolean => {
    const known = failedDescendants.get(id)
    if (known !== undefined) return known
    const seen = new Set<string>()
    const stack = [id]
    let found = false
    while (stack.length > 0 && !found) {
      const current = stack.pop() as string
      if (seen.has(current)) continue
      seen.add(current)
      if (isFailedState(nodes[current].state)) found = true
      else stack.push(...childrenOf(current))
    }
    failedDescendants.set(id, found)
    return found
  }

  const placed = new Set<string>()
  const anchors = new Map<string, { x: number; y: number }>()
  const pending: PendingEdge[] = []
  const direct: PlacedEdge[] = []
  const tasks: PlacedTask[] = []
  const containers: PlacedContainer[] = []
  const more: PlacedMore[] = []
  const notRun: PlacedNotRun[] = []
  let seq = 0

  const place = (id: string, x: number, y: number): Extent =>
    isContainer(id) ? placeContainer(id, x, y) : placeTask(id, x, y)

  function placeTask(id: string, x: number, y: number): Extent {
    placed.add(id)
    const mySeq = seq++
    const kids = byStart(childrenOf(id)).filter((kid) => !cells.has(kid))
    let bottom = y
    let drawn = 0
    for (const kid of kids) {
      if (placed.has(kid) || callbacks.has(kid)) continue
      const extent = place(kid, x + TASK_W + GAP_X, bottom)
      bottom += extent.h + GAP_Y
      drawn++
    }
    const height = Math.max(TASK_H, drawn > 0 ? bottom - GAP_Y - y : 0)
    const top = y + (height - TASK_H) / 2
    tasks.push({ id, x, y: top, w: TASK_W, h: TASK_H, containerId: null, seq: mySeq })
    anchors.set(id, { x, y: top + TASK_H / 2 })
    for (const kid of kids) {
      pending.push({
        from: id,
        to: kid,
        x1: x + TASK_W,
        y1: top + TASK_H / 2,
        fan: kids.length > 1,
        junction: null,
      })
    }
    return { h: height }
  }

  function placeContainer(id: string, x: number, y: number): Extent {
    placed.add(id)
    const node = nodes[id]
    const mySeq = seq++
    const parts = partsOf(id)
    // A cell another container (or the root) already drew is not drawn twice
    const rows = parts.rows
      .map((row) => row.filter((cell) => !placed.has(cell)))
      .filter((row) => row.length > 0)
    const rowFailed = (row: string[]): boolean => row.some(hasFailure)
    const collapsible = rows.length > COLLAPSE_AT
    const isExpanded = collapsible && expanded.has(id)
    const collapsed = collapsible && !isExpanded
    const shown = collapsed
      ? rows.filter((row, index) => index < COLLAPSED_HEAD || rowFailed(row))
      : rows
    const hidden = collapsed ? rows.filter((row) => !shown.includes(row)) : []

    type Item = { row: string[] } | { more: string[][] } | { less: true }
    const items: Item[] = shown.map((row) => ({ row }))
    if (hidden.length > 0) items.push({ more: hidden })
    else if (collapsible) items.push({ less: true })

    const singleCells = shown.every((row) => row.length === 1)
    const columns = isExpanded && singleCells && shown.length > COLLAPSE_AT ? EXPANDED_COLUMNS : 1
    const trackRows = Math.max(1, Math.ceil(items.length / columns))
    const longest = Math.max(1, ...shown.map((row) => row.length))
    const inner =
      columns > 1
        ? columns * MEMBER_W + (columns - 1) * TRACK_GAP
        : longest * MEMBER_W + (longest - 1) * GAP_X
    const w = inner + 2 * PAD
    const h = HEAD + trackRows * MEMBER_H + (trackRows - 1) * TRACK_GAP + FOOT
    const hub = y + HEAD + (h - HEAD - FOOT) / 2
    const callbackId = callbackOf.get(id) ?? null

    // The members, row by row
    const tracks = new Map<number, PlacedTrack>()
    const memberEdges: Array<{ last: string; cy: number }> = []
    const shownCells: Array<{ cell: string; next: string | undefined }> = []
    items.forEach((item, index) => {
      const column = index % columns
      const ix = x + PAD + column * (MEMBER_W + TRACK_GAP)
      const iy = y + HEAD + Math.floor(index / columns) * (MEMBER_H + TRACK_GAP)
      const cy = iy + MEMBER_H / 2
      const track = tracks.get(cy) ?? { y: cy, ids: [], failed: false, railOut: false }
      tracks.set(cy, track)
      if ('row' in item) {
        item.row.forEach((cell, k) => {
          placed.add(cell)
          const cx = ix + k * (MEMBER_W + GAP_X)
          tasks.push({
            id: cell,
            x: cx,
            y: iy,
            w: MEMBER_W,
            h: MEMBER_H,
            containerId: id,
            seq: seq++,
          })
          anchors.set(cell, { x: cx, y: cy })
          track.ids.push(cell)
          if (isFailedState(nodes[cell].state)) track.failed = true
          const next = item.row[k + 1]
          shownCells.push({ cell, next })
          if (next !== undefined) {
            direct.push({
              from: cell,
              to: next,
              x1: cx + MEMBER_W,
              y1: cy,
              x2: cx + MEMBER_W + GAP_X,
              y2: cy,
              failed: isFailedState(nodes[next].state),
              notRun: false,
              fan: false,
              junction: null,
            })
          }
        })
        memberEdges.push({ last: item.row[item.row.length - 1], cy })
      } else if ('more' in item) {
        const states = item.more.map((row) => nodes[row[row.length - 1]].state)
        more.push({
          containerId: id,
          kind: 'more',
          x: ix,
          y: iy,
          w: MEMBER_W,
          h: MEMBER_H,
          hiddenCount: item.more.length,
          hiddenIds: item.more.flat(),
          summary: describeStates(states),
          seq: seq++,
        })
      } else {
        more.push({
          containerId: id,
          kind: 'less',
          x: ix,
          y: iy,
          w: MEMBER_W,
          h: MEMBER_H,
          hiddenCount: 0,
          hiddenIds: [],
          summary: '',
          seq: seq++,
        })
      }
    })

    // Member counts are over every member, shown or not
    const memberStates = rows.map((row) => {
      const failedCell = row.find((cell) => isFailedState(nodes[cell].state))
      return nodes[failedCell ?? row[row.length - 1]].state
    })
    const lastNames = rows.map((row) => nodes[row[row.length - 1]].name)
    const sharedName =
      lastNames.length > 0 && lastNames.every((name) => name === lastNames[0])
        ? lastNameSegment(lastNames[0])
        : null
    const failedCount = memberStates.filter(isFailedState).length
    const succeeded = memberStates.filter((state) => state === 'SUCCESS').length

    // Outside, right of the container: the callback, a cell's own children, a container child
    const outsideX = x + w + GAP_X
    const outside: Array<{ id: string; from: string | null }> = []
    if (callbackId && !placed.has(callbackId)) outside.push({ id: callbackId, from: null })
    for (const { cell, next } of shownCells) {
      for (const kid of byStart(childrenOf(cell))) {
        if (kid !== next && !cells.has(kid) && !callbacks.has(kid))
          outside.push({ id: kid, from: cell })
      }
    }
    for (const head of parts.containerHeads) outside.push({ id: head, from: id })

    const notRunReason =
      typeOf(node) === 'CHORD' && !callbackId && node.not_run_reason ? node.not_run_reason : null
    let bottom = y
    let slot = y
    if (notRunReason) {
      // Centred on the hub when alone in the column, else the first thing in it
      const top = outside.length === 0 ? hub - NOT_RUN_H / 2 : slot
      const box: PlacedNotRun = {
        id: `${id}:not-run`,
        chordId: id,
        reason: notRunReason,
        text: notRunText(notRunReason, failedCount, rows.length),
        x: outsideX,
        y: top,
        w: NOT_RUN_W,
        h: NOT_RUN_H,
        seq: seq++,
      }
      notRun.push(box)
      anchors.set(box.id, { x: box.x, y: top + NOT_RUN_H / 2 })
      slot = top + NOT_RUN_H + GAP_Y
      bottom = top + NOT_RUN_H
      pending.push({ from: id, to: box.id, x1: x + w, y1: hub, fan: false, junction: null })
    } else if (outside.length === 1 && outside[0].id === callbackId) {
      slot = hub - TASK_H / 2
    }
    for (const out of outside) {
      if (placed.has(out.id)) continue
      const extent = place(out.id, outsideX, slot)
      slot += extent.h + GAP_Y
      bottom = Math.max(bottom, slot - GAP_Y)
    }

    // Edges: a member to the callback (rejoining at the container's right edge), a cell to its child
    if (callbackId) {
      for (const { last, cy } of memberEdges) {
        pending.push({
          from: last,
          to: callbackId,
          x1: x + w - PAD,
          y1: cy,
          fan: false,
          junction: { x: x + w, y: hub },
        })
        const track = tracks.get(cy)
        if (track) track.railOut = true
      }
    }
    for (const out of outside) {
      if (out.from === null) continue
      const fromCell = anchors.get(out.from)
      pending.push({
        from: out.from,
        to: out.id,
        x1: out.from === id ? x + w : (fromCell?.x ?? x) + MEMBER_W,
        y1: out.from === id ? hub : (fromCell?.y ?? hub),
        fan: false,
        junction: null,
      })
    }

    const height = Math.max(h, bottom - y)
    const mark: PlacedContainer = {
      id,
      type: typeOf(node) as Exclude<NodeType, 'TASK'>,
      x,
      y,
      w,
      h,
      hub,
      memberCount: rows.length,
      sharedName,
      succeeded,
      failed: failedCount,
      other: rows.length - succeeded - failedCount,
      tracks: [...tracks.values()],
      callbackId,
      collapsible,
      expanded: isExpanded,
      seq: mySeq,
    }
    containers.push(mark)
    anchors.set(id, { x, y: hub })
    return { h: height }
  }

  place(rootId, MARGIN, MARGIN)

  const edges: PlacedEdge[] = [...direct]
  for (const edge of pending) {
    const target = anchors.get(edge.to)
    if (!target) continue
    const targetNode = nodes[edge.to]
    edges.push({
      from: edge.from,
      to: edge.to,
      x1: edge.x1,
      y1: edge.y1,
      x2: target.x,
      y2: target.y,
      // A member to callback edge lights when its member failed, not when the callback did
      failed: edge.junction
        ? isFailedState(nodes[edge.from].state)
        : targetNode !== undefined && isFailedState(targetNode.state),
      notRun: edge.to.endsWith(':not-run'),
      fan: edge.fan,
      junction: edge.junction,
    })
  }

  const bySeq = <T extends { seq: number }>(list: T[]): T[] => list.sort((a, b) => a.seq - b.seq)
  let right = 0
  let bottomMost = 0
  for (const box of [...tasks, ...containers, ...more, ...notRun]) {
    right = Math.max(right, box.x + box.w)
    bottomMost = Math.max(bottomMost, box.y + box.h)
  }
  return {
    width: right + MARGIN,
    height: bottomMost + MARGIN,
    tasks: bySeq(tasks),
    containers: bySeq(containers),
    more: bySeq(more),
    notRun: bySeq(notRun),
    // Left to right, top to bottom: a stable order for the DOM and for tests
    edges: edges.sort((a, b) => a.x1 - b.x1 || a.y1 - b.y1 || a.y2 - b.y2),
  }
}
