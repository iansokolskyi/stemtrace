/**
 * Pure model of the Flow page's Timeline (no React): where each bar starts, waits and ends, the
 * axis, the rows and the critical path. The rules are written out in the PR that added this file.
 */
import type { GraphNode } from '@/api/client'
import { formatSpan, formatTime } from '@/utils/format'
import type { FlowLayout } from './layoutFlow'

/** The axis never spans less than this, so one task still has a scale. */
const MIN_SPAN_MS = 1000
/** Ticks are placed so that at most this many steps fit on the axis. */
const TICK_STEPS = 5
/** An open bar may run to `settledEnd + max(OPEN_CAP_FACTOR * settledSpan, OPEN_CAP_FLOOR_MS)`. */
export const OPEN_CAP_FACTOR = 3
export const OPEN_CAP_FLOOR_MS = 30_000
/** The Timeline indents a row by its depth up to this many levels. */
export const MAX_DEPTH = 4

/**
 * One row's bar in epoch milliseconds, before the axis is known (`end` null: still open).
 * `from <= scheduledTo <= leadTo <= runFrom <= end`. [from, scheduledTo] is a countdown or ETA,
 * [scheduledTo, leadTo] queue wait, [leadTo, runFrom] earlier attempts of a retried task and
 * [runFrom, end] the last run. A row that never ran (`hasRun` false) is a lead-in only.
 */
export interface RawBar {
  from: number
  scheduledTo: number
  leadTo: number
  runFrom: number
  end: number | null
  hasRun: boolean
}

/** A bar once the axis fixes the open ends: every field finite, `end` at most the axis end. */
export interface Bar {
  from: number
  scheduledTo: number
  leadTo: number
  runFrom: number
  end: number
  hasRun: boolean
  /** Still going: runs (or waits) to the axis end. */
  open: boolean
}

export interface Tick {
  t: number
  label: string
}

export interface Axis {
  t0: number
  t1: number
  ticks: Tick[]
}

export interface TimelineRow {
  /** Stable React key: `t:<task>`, `m:<container>` or `n:<chord>`. */
  key: string
  kind: 'task' | 'more' | 'notRun'
  /** What a click selects: the task; the chord for a not-run row; the container for "+N more". */
  id: string
  /** The box in the diagram the row centres: `<chord>:not-run` for a not-run row. */
  centreId: string
  depth: number
  critical: boolean
  /** No bar: the callback that did not run, or a task without a first_seen. */
  bar: Bar | null
  /** Tasks a "+N more" row stands for. */
  hidden: number
}

export interface Timeline {
  rows: TimelineRow[]
  axis: Axis | null
  /** The critical path: task ids, the containers it passes and `<container>:more` for an aggregate. */
  path: string[]
  /** When each timed task (and `<container>:more`) ends, open ends at the axis end. */
  ends: Map<string, number>
}

const toMs = (iso: string | null | undefined): number | null => {
  if (!iso) return null
  const time = Date.parse(iso)
  return Number.isNaN(time) ? null : time
}

/** A duration the API sent: a finite number of at least 0, else unknown (it clamps to 0 itself). */
const span = (value: number | null | undefined): number | null =>
  typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : null

const clamp = (value: number, low: number, high: number): number =>
  Math.min(Math.max(value, low), high)

/**
 * The bar of a task from the API's own fields (`duration_ms` is `last_updated - first_seen`, so
 * nothing in it is new), or null without a usable `first_seen`. Every instant is held inside
 * [first_seen, last_updated]: a clock that disagrees shortens a bar, it never moves one outside.
 */
export function barOf(node: GraphNode): RawBar | null {
  const first = toMs(node.first_seen)
  if (first === null) return null
  const last = Math.max(toMs(node.last_updated) ?? first, first)
  const wait = span(node.queue_wait_ms)
  const runtime = span(node.runtime_ms)
  const total = span(node.total_runtime_ms)
  const eta = toMs(node.eta)
  // queue_wait_ms counts from the ETA when it is later than the send
  const waitFrom = eta === null ? first : Math.max(first, eta)
  const firstStart = (high: number): number =>
    clamp(wait === null ? first : waitFrom + wait, first, high)

  if (node.state === 'PENDING' || node.state === 'RECEIVED') {
    return {
      from: first,
      scheduledTo: waitFrom,
      leadTo: first,
      runFrom: first,
      end: null,
      hasRun: false,
    }
  }

  if (runtime !== null) {
    // The last run is the last `runtime` ms before the final event, on the worker's clock
    const runFrom = clamp(last - runtime, first, last)
    const retried = total !== null && total > runtime
    const leadTo = retried ? firstStart(runFrom) : runFrom
    return {
      from: first,
      scheduledTo: clamp(waitFrom, first, leadTo),
      leadTo,
      runFrom,
      end: last,
      hasRun: true,
    }
  }

  if (node.state === 'STARTED' || node.state === 'RETRY' || wait !== null) {
    const start = firstStart(last)
    return {
      from: first,
      scheduledTo: clamp(waitFrom, first, start),
      leadTo: start,
      runFrom: start,
      end: node.state === 'STARTED' ? null : last,
      hasRun: true,
    }
  }

  // Ended without a recorded start: it waited, and nothing says it ran
  return {
    from: first,
    scheduledTo: clamp(waitFrom, first, last),
    leadTo: last,
    runFrom: last,
    end: last,
    hasRun: false,
  }
}

/** The bar of a "+N more" row: from the first hidden start to the last hidden end. */
function aggregate(bars: RawBar[]): RawBar | null {
  if (bars.length === 0) return null
  const from = Math.min(...bars.map((bar) => bar.from))
  const runs = bars.filter((bar) => bar.hasRun)
  const open = bars.some((bar) => bar.end === null && (runs.length === 0 || bar.hasRun))
  const end = open ? null : Math.max(...(runs.length > 0 ? runs : bars).map((bar) => bar.end ?? 0))
  if (runs.length === 0) {
    return {
      from,
      scheduledTo: from,
      leadTo: end ?? from,
      runFrom: end ?? from,
      end,
      hasRun: false,
    }
  }
  const start = Math.min(...runs.map((bar) => bar.runFrom))
  return { from, scheduledTo: from, leadTo: start, runFrom: start, end, hasRun: true }
}

const moreId = (containerId: string): string => `${containerId}:more`

/** Successors of each node by dependency: layout edges plus a container's members. */
function dependencies(layout: FlowLayout): {
  next: Map<string, string[]>
  seq: Map<string, number>
} {
  const seq = new Map<string, number>()
  for (const task of layout.tasks) seq.set(task.id, task.seq)
  for (const box of layout.containers) seq.set(box.id, box.seq)
  for (const block of layout.more)
    if (block.kind === 'more') seq.set(moreId(block.containerId), block.seq)

  const out = new Map<string, string[]>()
  const add = (from: string, to: string): void => {
    const list = out.get(from) ?? []
    if (!list.includes(to)) list.push(to)
    out.set(from, list)
  }
  const containerOf = new Map(layout.tasks.map((task) => [task.id, task.containerId]))
  const inRow = new Set<string>()
  for (const edge of layout.edges) {
    if (edge.notRun) continue
    add(edge.from, edge.to)
    const owner = containerOf.get(edge.from)
    if (owner && owner === containerOf.get(edge.to)) inRow.add(edge.to)
  }
  // The tracks inside a container are not edges: its members are the heads of its rows
  for (const task of layout.tasks) {
    if (task.containerId && !inRow.has(task.id)) add(task.containerId, task.id)
  }
  for (const block of layout.more) {
    if (block.kind !== 'more') continue
    add(block.containerId, moreId(block.containerId))
    const callback = layout.containers.find((box) => box.id === block.containerId)?.callbackId
    if (callback) add(moreId(block.containerId), callback)
  }
  for (const [from, list] of out) {
    out.set(
      from,
      list.sort((a, b) => (seq.get(a) ?? 0) - (seq.get(b) ?? 0)),
    )
  }
  return { next: out, seq }
}

/**
 * The critical path: the dependency chain from the root to the timed task that ended last, found
 * by tracing back from that task. Dependencies are the layout's edges; no step needs to end after
 * the one before it, because a successor can be stamped a few milliseconds earlier than the task
 * it followed (Celery sends the next link before stemtrace stamps the SUCCESS; hosts' clocks
 * differ), and a rule on end order would cut the path there. Containers and tasks without times
 * are passed. Where a node has several predecessors (headers into a callback) the one that ended
 * last is taken. Ties on the last end go to the node still open, then the one the layout reached
 * first. `ends` holds each timed node's end (`<container>:more` included); `open` those still going.
 * Only nodes the root reaches count. Empty when nothing reachable is timed.
 */
export function criticalPath(
  layout: FlowLayout,
  ends: ReadonlyMap<string, number>,
  rootId: string,
  open: ReadonlySet<string> = new Set(),
): string[] {
  const { next, seq } = dependencies(layout)
  const order = (id: string): number => seq.get(id) ?? Number.MAX_SAFE_INTEGER
  const depth = new Map<string, number>([[rootId, 0]])
  const preds = new Map<string, string[]>()
  const queue = [rootId]
  for (let at = 0; at < queue.length; at++) {
    const id = queue[at]
    for (const successor of next.get(id) ?? []) {
      preds.set(successor, [...(preds.get(successor) ?? []), id])
      if (!depth.has(successor)) {
        depth.set(successor, (depth.get(id) as number) + 1)
        queue.push(successor)
      }
    }
  }

  // The task that ended last; of equal ends the one still open, then the first reached
  let last: string | null = null
  for (const id of queue) {
    const end = ends.get(id)
    if (end === undefined) continue
    const best = last === null ? undefined : ends.get(last)
    if (
      last === null ||
      best === undefined ||
      end > best ||
      (end === best && (open.has(id) !== open.has(last) ? open.has(id) : order(id) < order(last)))
    ) {
      last = id
    }
  }
  if (last === null) return []

  // Back to the root, only through nodes closer to it, so the walk ends
  const path = [last]
  for (let current = last; current !== rootId; ) {
    const closer = (preds.get(current) ?? []).filter(
      (id) => (depth.get(id) as number) < (depth.get(current) as number),
    )
    let pick = closer[0]
    for (const id of closer) {
      const end = ends.get(id) ?? Number.NEGATIVE_INFINITY
      const had = ends.get(pick) ?? Number.NEGATIVE_INFINITY
      if (end > had || (end === had && order(id) < order(pick))) pick = id
    }
    path.unshift(pick)
    current = pick
  }
  return path
}

/** The first multiple-of-1/2/5 step that fits `raw`. */
function niceStep(raw: number): number {
  const power = 10 ** Math.floor(Math.log10(raw))
  const fraction = raw / power
  return (fraction <= 1 ? 1 : fraction <= 2 ? 2 : fraction <= 5 ? 5 : 10) * power
}

function axisOf(t0: number, t1: number): Axis {
  const step = niceStep((t1 - t0) / TICK_STEPS)
  const ticks: Tick[] = []
  for (let offset = 0; offset <= t1 - t0; offset += step) {
    ticks.push({
      t: t0 + offset,
      label:
        offset === 0 ? `start ${formatTime(new Date(t0).toISOString())}` : `+${formatSpan(offset)}`,
    })
  }
  return { t0, t1, ticks }
}

/**
 * The Timeline for a layout: a row per drawn task, "+N more" block and not-run box, in the
 * diagram's order; the axis over every bar (hidden tasks too); the critical path. Open bars run to
 * `now`, held at the latest event so a clock behind the server cannot shrink them; a flow with
 * nothing open ignores `now`.
 */
export function buildTimeline(
  nodes: Record<string, GraphNode>,
  layout: FlowLayout,
  rootId: string,
  now: number,
): Timeline {
  type Item =
    | { kind: 'task'; seq: number; id: string }
    | { kind: 'more'; seq: number; id: string; hiddenIds: string[] }
    | { kind: 'notRun'; seq: number; id: string; chordId: string }
  const items: Item[] = [
    ...layout.tasks.map((task): Item => ({ kind: 'task', seq: task.seq, id: task.id })),
    ...layout.more.flatMap((block): Item[] =>
      block.kind === 'more'
        ? [{ kind: 'more', seq: block.seq, id: block.containerId, hiddenIds: block.hiddenIds }]
        : [],
    ),
    ...layout.notRun.map(
      (box): Item => ({ kind: 'notRun', seq: box.seq, id: box.id, chordId: box.chordId }),
    ),
  ].sort((a, b) => a.seq - b.seq)

  const raw = new Map<string, RawBar | null>()
  for (const item of items) {
    if (item.kind === 'task') raw.set(item.id, nodes[item.id] ? barOf(nodes[item.id]) : null)
    else if (item.kind === 'more') {
      const hidden = item.hiddenIds.flatMap((id) => (nodes[id] ? (barOf(nodes[id]) ?? []) : []))
      raw.set(moreId(item.id), aggregate(hidden))
    }
  }

  const bars = [...raw.values()].flatMap((bar) => (bar ? [bar] : []))
  if (bars.length === 0) {
    return {
      rows: items.map((item) => rowOf(item, null, 0, false)),
      axis: null,
      path: [],
      ends: new Map(),
    }
  }
  const t0 = Math.min(...bars.map((bar) => bar.from))
  const settled = Math.max(...bars.map((bar) => (bar.end === null ? bar.runFrom : bar.end)))
  const anyOpen = bars.some((bar) => bar.end === null)
  const t1 = Math.max(settled, anyOpen && Number.isFinite(now) ? now : settled, t0 + MIN_SPAN_MS)
  const axis = axisOf(t0, t1)

  const resolved = new Map<string, Bar>()
  for (const [id, bar] of raw) {
    if (!bar) continue
    const open = bar.end === null
    const end = bar.end ?? t1
    const leadTo = bar.hasRun ? bar.leadTo : end
    resolved.set(id, {
      from: bar.from,
      scheduledTo: Math.min(bar.scheduledTo, leadTo),
      leadTo,
      runFrom: bar.hasRun ? bar.runFrom : end,
      end,
      hasRun: bar.hasRun,
      open,
    })
  }
  const ends = new Map<string, number>()
  const stillOpen = new Set<string>()
  for (const [id, bar] of resolved) {
    if (!bar.hasRun) continue
    ends.set(id, bar.end)
    if (bar.open) stillOpen.add(id)
  }

  const path = criticalPath(layout, ends, rootId, stillOpen)
  const onPath = new Set(path)
  const depths = depthsOf(layout, rootId)

  return {
    rows: items.map((item) => {
      const key = item.kind === 'more' ? moreId(item.id) : item.id
      const depth =
        item.kind === 'notRun' ? (depths.get(item.chordId) ?? 0) : (depths.get(key) ?? 0)
      return rowOf(item, resolved.get(key) ?? null, depth, onPath.has(key))
    }),
    axis,
    path,
    ends,
  }
}

function rowOf(
  item: { kind: 'task' | 'more' | 'notRun'; id: string; hiddenIds?: string[] },
  bar: Bar | null,
  depth: number,
  critical: boolean,
): TimelineRow {
  if (item.kind === 'more') {
    return {
      key: `m:${item.id}`,
      kind: 'more',
      id: item.id,
      centreId: item.id,
      depth,
      critical,
      bar,
      hidden: item.hiddenIds?.length ?? 0,
    }
  }
  if (item.kind === 'notRun') {
    const chordId = item.id.slice(0, item.id.length - ':not-run'.length)
    return {
      key: `n:${chordId}`,
      kind: 'notRun',
      id: chordId,
      centreId: item.id,
      depth,
      critical: false,
      bar: null,
      hidden: 0,
    }
  }
  return {
    key: `t:${item.id}`,
    kind: 'task',
    id: item.id,
    centreId: item.id,
    depth,
    critical,
    bar,
    hidden: 0,
  }
}

/**
 * How far in each row indents: a child sits one level below its parent; a container's members,
 * its "+N more" row and its callback sit at the container's own level.
 */
function depthsOf(layout: FlowLayout, rootId: string): Map<string, number> {
  const { next } = dependencies(layout)
  const incoming = new Map<string, string[]>()
  for (const [from, list] of next)
    for (const to of list) incoming.set(to, [...(incoming.get(to) ?? []), from])
  const containers = new Map(layout.containers.map((box) => [box.id, box]))
  const callbackOwner = new Map<string, string>()
  for (const box of layout.containers) if (box.callbackId) callbackOwner.set(box.callbackId, box.id)

  const order: Array<{ id: string; seq: number }> = [
    ...layout.tasks.map((task) => ({ id: task.id, seq: task.seq })),
    ...layout.containers.map((box) => ({ id: box.id, seq: box.seq })),
    ...layout.more.flatMap((block) =>
      block.kind === 'more' ? [{ id: moreId(block.containerId), seq: block.seq }] : [],
    ),
  ].sort((a, b) => a.seq - b.seq)

  const depth = new Map<string, number>()
  for (const { id } of order) {
    const owner = callbackOwner.get(id)
    if (id === rootId) depth.set(id, 0)
    else if (owner !== undefined && depth.has(owner)) depth.set(id, depth.get(owner) as number)
    else {
      const levels = (incoming.get(id) ?? []).flatMap((from) =>
        depth.has(from) ? [(depth.get(from) as number) + (containers.has(from) ? 0 : 1)] : [],
      )
      depth.set(id, levels.length > 0 ? Math.min(...levels) : 0)
    }
  }
  return depth
}
