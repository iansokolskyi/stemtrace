import { Link } from '@tanstack/react-router'
import { clsx } from 'clsx'
import type { ReactElement } from 'react'
import { useRecentExecutions } from '@/api/queries'
import { columnsFor } from '@/components/executions/columns'
import { Panel } from '@/components/task/Panel'
import { StatusChip } from '../StatusChip'
import { TaskName } from '../TaskName'
import { NUM, TD, TH } from './workers'

interface RecentRunsProps {
  hostname: string
}

/** The worker's latest runs: the Executions columns that have a value, each row opening its task. */
export function RecentRuns({ hostname }: RecentRunsProps): ReactElement {
  const { data, isLoading, error } = useRecentExecutions(hostname)
  const runs = data?.tasks ?? []
  // The Worker column would repeat this page's own host on every row
  const columns = columnsFor(runs).filter((column) => column.id !== 'worker')

  return (
    <Panel
      title="Recent executions"
      aside={runs.length > 0 && <span className="text-xs text-muted">latest {runs.length}</span>}
    >
      {isLoading ? (
        <p className="m-0 px-4 pt-2 pb-4 text-xs text-muted">Loading executions...</p>
      ) : error ? (
        <p className="m-0 px-4 pt-2 pb-4 text-xs font-semibold text-failure-t">
          Failed to load executions
        </p>
      ) : runs.length === 0 ? (
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
                    <td key={column.id} className={clsx(TD, column.align === 'right' && NUM)}>
                      {column.id === 'state' ? (
                        <StatusChip state={run.state} title={run.exception_type ?? undefined} />
                      ) : column.id === 'name' ? (
                        <Link
                          to="/tasks/$taskId"
                          params={{ taskId: run.task_id }}
                          aria-label={run.name}
                          className="block max-w-[320px] after:absolute after:inset-0"
                        >
                          <TaskName name={run.name} />
                        </Link>
                      ) : (
                        <span
                          className={clsx(
                            column.id === 'queue' && 'font-mono',
                            (column.id === 'updated' || column.id === 'queue_wait') && 'text-muted',
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
