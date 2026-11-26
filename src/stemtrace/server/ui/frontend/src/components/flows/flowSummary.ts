import type { GraphNode } from '@/api/client'

/** The root task's own name segment: the flow's title. */
export function flowTitle(name: string): string {
  return name.slice(name.lastIndexOf('.') + 1)
}

/**
 * The title of a GROUP or CHORD box: "Chord of 12 fetch", or "Group of 3 tasks" when the members
 * share no name. The flow page's box and the Flows list both draw it.
 */
export function containerTitle(
  type: 'GROUP' | 'CHORD',
  memberCount: number,
  sharedName: string | null,
): string {
  return `${type === 'CHORD' ? 'Chord' : 'Group'} of ${memberCount} ${sharedName ?? 'tasks'}`
}

/**
 * The title of a flow's row: the container title for a GROUP or CHORD root, the root's own short
 * name for a TASK root and for a server that sends no member fields.
 */
export function flowRowTitle(graph: GraphNode): string {
  const count = graph.flow?.member_count
  if ((graph.node_type !== 'GROUP' && graph.node_type !== 'CHORD') || count == null) {
    return flowTitle(graph.name)
  }
  return containerTitle(graph.node_type, count, graph.flow?.member_name ?? null)
}

/**
 * The summary line of a flow row: "50 tasks, 1 failed, chord callback not run". A part that is
 * zero is left out; with no summary from the server the line is empty.
 */
export function flowSummary(flow: GraphNode['flow']): string {
  if (!flow) return ''
  const failed = (flow.state_counts.FAILURE ?? 0) + (flow.state_counts.REJECTED ?? 0)
  const parts: string[] = []
  if (flow.task_count > 0) {
    parts.push(`${flow.task_count} ${flow.task_count === 1 ? 'task' : 'tasks'}`)
  }
  if (failed > 0) parts.push(`${failed} failed`)
  if (flow.not_run_count > 0) parts.push('chord callback not run')
  return parts.join(', ')
}
