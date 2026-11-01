import type { GraphNode } from '@/api/client'

/** The root task's own name segment: the flow's title. */
export function flowTitle(name: string): string {
  return name.slice(name.lastIndexOf('.') + 1)
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
