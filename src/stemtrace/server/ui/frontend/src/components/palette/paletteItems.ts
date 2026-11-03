import type { GraphNode, RegisteredTask, TaskDetailResponse, Worker } from '@/api/client'
import { flowSummary, flowTitle } from '@/components/flows/flowSummary'
import { STATUS_LOOK } from '@/components/registry/registry'
import { stateLook } from '@/components/StatusChip'
import { hostsOf } from '@/components/workers/workers'
import { fuzzy } from './fuzzy'

/** What a result opens. */
export type PaletteKind = 'task' | 'name' | 'flow' | 'worker'

export interface PaletteItem {
  /** Unique across the list: kind and id. */
  key: string
  kind: PaletteKind
  /** The group heading the item is listed under. */
  group: string
  /** The task ID, task name, flow root ID or hostname the item opens. */
  id: string
  label: string
  /** Second line of the row: always says the state in words. */
  desc: string
}

/** What the palette searches, as loaded; any part may be missing. */
export interface PaletteSources {
  names?: RegisteredTask[]
  flows?: GraphNode[]
  workers?: Worker[]
  /** The task the server returned for the query as an exact ID, if any. */
  task?: TaskDetailResponse['task'] | null
}

/**
 * A query that can be a task ID (hex digits and dashes, 8 or more). Shorter text is far more
 * likely a word ("dead", "cafe"), and each lookup is a request.
 */
export const TASK_ID_SHAPE = /^[0-9a-f-]{8,}$/i

const LIMITS = { names: 6, flows: 4, workers: 5 }

const flowItem = (flow: GraphNode): PaletteItem => ({
  key: `flow:${flow.task_id}`,
  kind: 'flow',
  group: 'Flows',
  id: flow.task_id,
  label: flowTitle(flow.name),
  desc: flowSummary(flow.flow) || flowTitle(flow.name),
})

const workerItem = (hostname: string, status: Worker['status']): PaletteItem => ({
  key: `worker:${hostname}`,
  kind: 'worker',
  group: 'Workers',
  id: hostname,
  label: hostname,
  desc: status === 'online' ? 'Online' : 'Offline',
})

/** The best `limit` of `list` by fuzzy score of `text`; items that do not match are dropped. */
function best<T>(list: T[], query: string, text: (item: T) => string, limit: number): T[] {
  return list
    .map((item) => ({ item, score: fuzzy(query, text(item)) }))
    .filter((x) => x.score >= 0)
    .sort((a, b) => b.score - a.score)
    .slice(0, limit)
    .map((x) => x.item)
}

/**
 * The palette's rows for `query`. No query lists every flow and worker. Otherwise: the task with
 * that exact ID, then the best task names, flows and workers by fuzzy match.
 */
export function paletteItems(query: string, sources: PaletteSources): PaletteItem[] {
  const q = query.trim()
  const { names = [], flows = [], workers = [], task } = sources
  const hosts = hostsOf(workers)
  if (!q) {
    return [...flows.map(flowItem), ...hosts.map((h) => workerItem(h.hostname, h.leading.status))]
  }

  const items: PaletteItem[] = []
  if (task && TASK_ID_SHAPE.test(q)) {
    items.push({
      key: `task:${task.task_id}`,
      kind: 'task',
      group: 'Task IDs',
      id: task.task_id,
      label: task.task_id,
      desc: `${task.name.slice(task.name.lastIndexOf('.') + 1)}, ${stateLook(task.state).word}`,
    })
  }
  for (const entry of best(names, q, (n) => n.name, LIMITS.names)) {
    items.push({
      key: `name:${entry.name}`,
      kind: 'name',
      group: 'Task names',
      id: entry.name,
      label: entry.name,
      desc: [entry.task_group, STATUS_LOOK[entry.status].word].filter(Boolean).join(', '),
    })
  }
  items.push(...best(flows, q, (f) => flowTitle(f.name), LIMITS.flows).map(flowItem))
  for (const host of best(hosts, q, (h) => h.hostname, LIMITS.workers)) {
    items.push(workerItem(host.hostname, host.leading.status))
  }
  return items
}
