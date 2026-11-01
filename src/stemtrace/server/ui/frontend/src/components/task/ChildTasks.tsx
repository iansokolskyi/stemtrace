import { Link } from '@tanstack/react-router'
import type { ReactElement } from 'react'
import type { TaskNode } from '@/api/client'
import { StatusChip } from '@/components/StatusChip'
import { shortestUniqueSuffixes } from '@/utils/taskNames'
import { Panel } from './Panel'

/**
 * The tasks this one started, as links. Names are the shortest suffix that tells the children
 * apart (`billing.send` against `email.send`); the full name is on hover. Nothing when there are none.
 */
export function ChildTasks({ tasks }: { tasks: TaskNode[] }): ReactElement | null {
  if (tasks.length === 0) return null
  const labels = shortestUniqueSuffixes(tasks.map((child) => child.name))

  return (
    <Panel title="Child tasks" aside={<span className="text-xs text-muted">{tasks.length}</span>}>
      <ul className="m-0 list-none px-2 pt-0 pb-2.5">
        {tasks.map((child) => (
          <li key={child.task_id}>
            <Link
              to="/tasks/$taskId"
              params={{ taskId: child.task_id }}
              className="flex min-w-0 items-center justify-between gap-3 rounded-[3px] px-2 py-1.5 hover:bg-hover"
            >
              <span className="flex min-w-0 items-baseline gap-2">
                <span title={child.name} className="min-w-0 truncate font-mono text-xs">
                  {labels.get(child.name) ?? child.name}
                </span>
                <span className="flex-none font-mono text-xs text-muted">
                  {child.task_id.slice(0, 8)}
                </span>
              </span>
              <StatusChip state={child.state} />
            </Link>
          </li>
        ))}
      </ul>
    </Panel>
  )
}
