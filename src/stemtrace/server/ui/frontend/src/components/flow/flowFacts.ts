import type { FlowSummary, GraphNode } from '@/api/client'
import { formatSpan } from '@/utils/format'
import { isFailedState } from './layoutFlow'

/** The colour family of a state (the `s-*` classes of the diagram). */
export type Tone = 'success' | 'failure' | 'retry' | 'running' | 'pending'

export function toneOf(state: string): Tone {
  switch (state) {
    case 'SUCCESS':
      return 'success'
    case 'FAILURE':
    case 'REJECTED':
      return 'failure'
    case 'RETRY':
      return 'retry'
    case 'STARTED':
      return 'running'
    default:
      return 'pending'
  }
}

/** First seen, missing last; the server's order breaks ties. */
function byFirstSeen(a: GraphNode, b: GraphNode): number {
  const at = a.first_seen ? Date.parse(a.first_seen) : Number.POSITIVE_INFINITY
  const bt = b.first_seen ? Date.parse(b.first_seen) : Number.POSITIVE_INFINITY
  return at === bt ? 0 : at < bt ? -1 : 1
}

/** The task that failed first (FAILURE or REJECTED, by first seen); undefined when none failed. */
export function firstFailure(nodes: Record<string, GraphNode>): GraphNode | undefined {
  return Object.values(nodes)
    .filter((node) => (node.node_type ?? 'TASK') === 'TASK' && isFailedState(node.state))
    .sort(byFirstSeen)[0]
}

// Worst first, as the server's FLOW_STATE_ORDER; a rejected task counts as failed and one
// that has not finished as running
const FLOW_ORDER = ['FAILURE', 'REVOKED', 'RETRY', 'STARTED', 'IGNORED', 'SUCCESS']
const FLOW_STATE_OF: Record<string, string> = {
  REJECTED: 'FAILURE',
  PENDING: 'STARTED',
  RECEIVED: 'STARTED',
}

/**
 * The summary the flows list gets from the server (`flow`), computed from a full graph, which
 * carries none: task counts, the worst state and the chords whose callback did not run.
 */
export function summarizeFlow(nodes: Record<string, GraphNode>, rootId: string): FlowSummary {
  const stateCounts: Record<string, number> = {}
  let taskCount = 0
  let notRun = 0
  for (const node of Object.values(nodes)) {
    if (node.not_run_reason) notRun += 1
    if ((node.node_type ?? 'TASK') !== 'TASK') continue
    taskCount += 1
    stateCounts[node.state] = (stateCounts[node.state] ?? 0) + 1
  }
  const worst = FLOW_ORDER.find((state) =>
    Object.keys(stateCounts).some((own) => (FLOW_STATE_OF[own] ?? own) === state),
  )
  return {
    task_count: taskCount,
    state_counts: stateCounts,
    not_run_count: notRun,
    state: worst ?? nodes[rootId]?.state ?? 'PENDING',
  }
}

/** The second line of a task block: the exception class of a failure, then the runtime. */
export function taskSubtitle(node: GraphNode): string {
  const parts: string[] = []
  if (isFailedState(node.state) && node.exception_type) parts.push(node.exception_type)
  const runtime = formatSpan(node.runtime_ms)
  if (runtime) parts.push(runtime)
  else if (node.state === 'STARTED') parts.push('running')
  else if (node.state === 'PENDING' || node.state === 'RECEIVED')
    parts.push(node.state.toLowerCase())
  return parts.join(', ')
}

/** `text` cut to `max` characters with an ellipsis. */
export function truncate(text: string, max: number): string {
  return text.length > max ? `${text.slice(0, max - 1)}…` : text
}

/** `text` on up to two lines of `max` characters, the second cut with an ellipsis. */
export function wrapTwo(text: string, max: number): [string, string] {
  const lines = ['', '']
  let at = 0
  for (const word of text.split(' ')) {
    if (`${lines[at]} ${word}`.trim().length > max && at < 1) at += 1
    lines[at] = `${lines[at]} ${word}`.trim()
  }
  return [lines[0], truncate(lines[1], max)]
}
