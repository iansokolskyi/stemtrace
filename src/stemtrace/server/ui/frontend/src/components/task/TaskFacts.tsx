import { Link } from '@tanstack/react-router'
import type { ReactElement, ReactNode } from 'react'
import type { TaskNode } from '@/api/client'
import { useCarriedRange } from '@/hooks/useCarriedRange'
import { formatDateTime, formatSpan } from '@/utils/format'
import { Panel } from './Panel'
import { retryCount } from './retryCount'

interface Fact {
  label: string
  value: ReactNode
}

/** The facts the API knows for this task, in reading order. A fact without a value is left out. */
function factsOf(task: TaskNode, workerLink: ReactNode): Fact[] {
  const retries = retryCount(task)
  const candidates: (Fact | false | null | undefined | '')[] = [
    task.task_group && { label: 'Group', value: task.task_group },
    task.tags && task.tags.length > 0 && { label: 'Tags', value: task.tags.join(', ') },
    task.hostname && { label: 'Worker', value: workerLink },
    task.queue && { label: 'Queue', value: task.queue },
    task.routing_key && { label: 'Routing key', value: task.routing_key },
    // Zero is the normal case and says nothing
    retries > 0 && { label: 'Retries', value: String(retries) },
    task.eta && { label: 'ETA', value: formatDateTime(task.eta) },
    task.first_seen && { label: 'First seen', value: formatDateTime(task.first_seen) },
    task.last_updated && { label: 'Last update', value: formatDateTime(task.last_updated) },
    task.duration_ms !== null && { label: 'Duration', value: formatSpan(task.duration_ms) },
  ]
  return candidates.filter((fact): fact is Fact => Boolean(fact))
}

interface TaskFactsProps {
  task: TaskNode
}

/** Where and how the task ran, as a label/value list with a row only for what is known. */
export function TaskFacts({ task }: TaskFactsProps): ReactElement | null {
  const carried = useCarriedRange()
  const workerLink = task.hostname && (
    <Link
      to="/executions"
      search={{ hostname: task.hostname, ...carried }}
      className="underline hover:text-muted"
    >
      {task.hostname}
    </Link>
  )
  const facts = factsOf(task, workerLink)
  if (facts.length === 0) return null

  return (
    <Panel title="Details">
      <dl className="m-0 grid grid-cols-[80px_minmax(0,1fr)] gap-x-3 gap-y-[7px] px-4 pt-2 pb-3.5 text-xs sm:grid-cols-[96px_minmax(0,1fr)]">
        {facts.map(({ label, value }) => (
          <div key={label} className="contents">
            <dt className="text-muted">{label}</dt>
            <dd className="m-0 [overflow-wrap:anywhere]">{value}</dd>
          </div>
        ))}
      </dl>
    </Panel>
  )
}
