import type { ReactElement } from 'react'
import type { ExecutionRow, FetchExecutionsParams } from '@/api/client'
import { usePanelRuns } from '@/api/overviewQueries'
import { StatusChip } from '@/components/StatusChip'
import { TaskName } from '@/components/TaskName'
import { formatAgo, formatCount, formatSpan, shortHost } from '@/utils/format'
import { useEntryState, usePanel } from './PanelContext'

/** Rows added by each "Show more". */
const PAGE = 40

const TH =
  'h-8 whitespace-nowrap border-b border-rule px-2.5 text-left text-xs font-semibold text-muted first:pl-2.5'

function RunRow({ run }: { run: ExecutionRow }): ReactElement {
  const panel = usePanel()
  return (
    <tr
      data-testid="panel-run"
      data-task-id={run.task_id}
      className="relative border-b border-rule last:border-b-0 hover:bg-hover"
    >
      <td className="px-2.5 py-1.5 align-top">
        <StatusChip state={run.state} />
      </td>
      <td className="max-w-[260px] px-2.5 py-1.5">
        <button
          type="button"
          onClick={() => panel.push({ kind: 'task', id: run.task_id })}
          className="block max-w-full text-left after:absolute after:inset-0"
        >
          <TaskName name={run.name} />
        </button>
        <div className="truncate text-[11.5px] text-muted">
          <span className="font-mono">{run.task_id.slice(0, 8)}</span>
          {run.task_group ? ` in ${run.task_group}` : ''}
          {run.hostname ? ` on ${shortHost(run.hostname)}` : ''}
        </div>
      </td>
      <td className="px-2.5 py-1.5 text-right text-xs whitespace-nowrap tabular-nums">
        {run.queue_wait_ms == null ? '' : formatSpan(run.queue_wait_ms)}
      </td>
      <td className="px-2.5 py-1.5 text-right text-xs whitespace-nowrap tabular-nums">
        {run.runtime_ms == null ? '' : formatSpan(run.runtime_ms)}
      </td>
      <td className="px-2.5 py-1.5 text-right text-xs whitespace-nowrap text-muted tabular-nums">
        {formatAgo(run.last_updated)}
      </td>
    </tr>
  )
}

interface RunRowsProps {
  filters: FetchExecutionsParams
  /** Rows to show at first. */
  initial?: number
}

/**
 * The runs behind a panel entry, newest first: the API's own filtering (the bar's span, the
 * state, the exception), the first rows and a "Show more" for the rest. A row opens the task in
 * the panel.
 */
export function RunRows({ filters, initial = PAGE }: RunRowsProps): ReactElement {
  const [limit, setLimit] = useEntryState('limit', initial)
  const { data, isError } = usePanelRuns(filters, limit)
  if (isError) return <p className="m-0 py-3 text-xs text-failure-t">Failed to load runs</p>
  if (!data) return <div className="h-24" aria-hidden="true" />
  if (data.tasks.length === 0)
    return <p className="m-0 py-4 text-center text-xs text-muted">No runs match.</p>
  return (
    <>
      <div className="overflow-x-auto rounded-[3px]">
        <table aria-label="Runs" className="w-full border-collapse">
          <thead>
            <tr>
              <th scope="col" className={TH}>
                State
              </th>
              <th scope="col" className={TH}>
                Task
              </th>
              <th scope="col" className={`${TH} text-right`}>
                Wait
              </th>
              <th scope="col" className={`${TH} text-right`}>
                Runtime
              </th>
              <th scope="col" className={`${TH} text-right`}>
                Updated
              </th>
            </tr>
          </thead>
          <tbody>
            {data.tasks.map((run) => (
              <RunRow key={run.task_id} run={run} />
            ))}
          </tbody>
        </table>
      </div>
      {data.total > data.tasks.length && (
        <button
          type="button"
          onClick={() => setLimit(limit + PAGE)}
          className="mt-2 inline-flex h-[30px] items-center rounded-[3px] bg-raised px-3 text-xs font-semibold hover:bg-press"
        >
          Show {Math.min(PAGE, data.total - data.tasks.length)} more of {formatCount(data.total)}
        </button>
      )}
    </>
  )
}
