import { Link } from '@tanstack/react-router'
import { clsx } from 'clsx'
import { type ReactElement, useState } from 'react'
import type { ExecutionRow } from '@/api/client'
import { type Column, columnsFor } from '@/components/executions/columns'
import { Panel } from '@/components/task/Panel'
import type { ViewState } from '@/hooks/useViewState'
import { LoadFailure } from '../Notice'
import { StatusChip } from '../StatusChip'
import { TableSkeleton } from '../TableSkeleton'
import { TaskName } from '../TaskName'
import { NUM, TD, TH } from './workers'

interface RecentRunsProps {
  runs: ExecutionRow[] | undefined
  /** The worker page's recent-runs query: its page shows the stale line. */
  view: ViewState
}

/** The worker's latest runs: the Executions columns that have a value, each row opening its task. */
export function RecentRuns({ runs: held, view }: RecentRunsProps): ReactElement {
  const runs = held ?? []
  // Decided once, from the first runs that arrive, like the Executions table: a refetch never
  // adds or drops a column. The Worker column would repeat this page's own host on every row.
  const [columns, setColumns] = useState<Column[] | null>(null)
  if (columns === null && runs.length > 0) {
    setColumns(columnsFor(runs).filter((column) => column.id !== 'worker'))
  }

  return (
    <Panel
      title="Recent executions"
      aside={runs.length > 0 && <span className="text-xs text-muted">latest {runs.length}</span>}
    >
      {view.phase === 'loading' ? (
        <TableSkeleton label="recent executions" rows={3} bare />
      ) : view.phase === 'error' ? (
        <LoadFailure what="recent executions" view={view} />
      ) : runs.length === 0 || !columns ? (
        <p className="m-0 px-4 pt-2 pb-5 text-xs text-muted">No executions from this worker.</p>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full min-w-[480px] border-collapse">
            <thead>
              <tr>
                {columns.map((column) => (
                  <th
                    key={column.id}
                    scope="col"
                    className={clsx(TH, column.align === 'right' && NUM)}
                  >
                    {column.label}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {runs.map((run) => (
                <tr key={run.task_id} className="relative border-t border-rule hover:bg-hover">
                  {columns.map((column) => (
                    <td
                      key={column.id}
                      className={clsx(
                        TD,
                        column.align === 'right' && NUM,
                        // The name takes the room the other columns leave, 260 px at least (like
                        // Executions' minmax(260px, 1fr)); max-w-0 lets it shrink below its text
                        column.id === 'name' && 'w-full max-w-0 min-w-[260px]',
                      )}
                    >
                      {column.id === 'state' ? (
                        <StatusChip state={run.state} title={run.exception_type ?? undefined} />
                      ) : column.id === 'name' ? (
                        <Link
                          to="/tasks/$taskId"
                          params={{ taskId: run.task_id }}
                          aria-label={run.name}
                          className="block after:absolute after:inset-0"
                        >
                          <TaskName name={run.name} />
                        </Link>
                      ) : (
                        <span
                          className={clsx(
                            column.id === 'queue' && 'font-mono',
                            (column.id === 'started' || column.id === 'queue_wait') && 'text-muted',
                          )}
                        >
                          {column.text?.(run)}
                        </span>
                      )}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Panel>
  )
}
