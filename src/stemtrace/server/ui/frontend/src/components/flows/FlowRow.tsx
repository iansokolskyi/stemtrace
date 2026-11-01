import { Link } from '@tanstack/react-router'
import type { Ref } from 'react'
import type { GraphNode } from '@/api/client'
import { StatusChip } from '@/components/StatusChip'
import { TaskName } from '@/components/TaskName'
import { formatAgo } from '@/utils/format'
import { flowSummary, flowTitle } from './flowSummary'

interface FlowRowProps {
  graph: GraphNode
  /** Set on the last row, so scrolling to it loads the next page. */
  rowRef?: Ref<HTMLAnchorElement>
}

/** One flow of the list: a link to its page with the state, title, summary, root and age. */
export function FlowRow({ graph, rowRef }: FlowRowProps) {
  const summary = flowSummary(graph.flow)
  return (
    <Link
      ref={rowRef}
      to="/flows/$rootId"
      params={{ rootId: graph.task_id }}
      data-testid="flow-row"
      className="grid w-full grid-cols-[auto_minmax(0,1fr)_auto] items-center gap-3 border-t border-rule px-4 py-3 first:border-t-0 hover:bg-hover"
    >
      {/* The summary's worst state; the root's own can say Success for a flow whose chord failed */}
      <StatusChip state={graph.flow?.state ?? graph.state} />
      <span className="min-w-0">
        <b className="block truncate text-[13px]">{flowTitle(graph.name)}</b>
        {summary && <span className="block text-muted">{summary}</span>}
        <span className="flex min-w-0 items-baseline gap-1.5 text-muted">
          root
          <TaskName name={graph.name} />
        </span>
      </span>
      <span className="text-right text-muted">{formatAgo(graph.first_seen)}</span>
    </Link>
  )
}
