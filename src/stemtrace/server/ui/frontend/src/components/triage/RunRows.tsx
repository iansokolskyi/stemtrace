import type { ReactElement } from 'react'
import type { ExecutionRow, FetchExecutionsParams, RunSort } from '@/api/client'
import { usePanelRuns } from '@/api/overviewQueries'
import { LoadFailure, MoreFailed } from '@/components/Notice'
import { OUTCOME_STATES } from '@/components/overview/model'
import { PageStale } from '@/components/PageStale'
import { StatusChip } from '@/components/StatusChip'
import { TableSkeleton } from '@/components/TableSkeleton'
import { TaskName } from '@/components/TaskName'
import { useViewState } from '@/hooks/useViewState'
import { formatAgo, formatCount, formatSpan, shortHost } from '@/utils/format'
import { useEntryView, usePanel } from './PanelContext'
import { MAX_MORE } from './panelState'

/** Rows added by each "Show more". */
const PAGE = 40

/** The most rows one request may ask for (`limit` of `GET /api/tasks`). */
const MAX_LIMIT = 500

const TH = 'th-band h-8 whitespace-nowrap px-2.5 text-left first:pl-2.5'

/** The tile's name for each order, as the marker on a run it leaves out says it. */
const TILE_NAME: Record<RunSort, string> = { runtime: 'p95 runtime', queue_wait: 'p95 queue wait' }

/**
 * Whether the p95 tile of `sort` takes this run as a sample: the server's rule (`stats.py`
 * `runtime_sample` and `queue_wait_sample`, with the `celery.` tasks left out). A running task
 * shows its live wait but is not one.
 */
function isCounted(run: ExecutionRow, sort: RunSort): boolean {
  if (run.name.startsWith('celery.')) return false
  if (sort === 'runtime') {
    return (run.state === 'SUCCESS' || run.state === 'FAILURE') && run.runtime_ms != null
  }
  return (OUTCOME_STATES as readonly string[]).includes(run.state) && run.queue_wait_ms != null
}

function RunRow({ run, sort }: { run: ExecutionRow; sort?: RunSort }): ReactElement {
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
        <div className="truncate text-meta text-muted">
          <span className="font-mono">{run.task_id.slice(0, 8)}</span>
          {run.task_group ? ` in ${run.task_group}` : ''}
          {run.hostname ? ` on ${shortHost(run.hostname)}` : ''}
        </div>
        {sort && !isCounted(run, sort) && (
          <div data-testid="panel-run-uncounted" className="text-meta text-muted italic">
            Not counted in the {TILE_NAME[sort]} tile
          </div>
        )}
      </td>
      <td className="px-2.5 py-1.5 text-right text-small whitespace-nowrap tabular-nums">
        {run.queue_wait_ms == null ? '' : formatSpan(run.queue_wait_ms)}
      </td>
      <td className="px-2.5 py-1.5 text-right text-small whitespace-nowrap tabular-nums">
        {run.runtime_ms == null ? '' : formatSpan(run.runtime_ms)}
      </td>
      <td className="px-2.5 py-1.5 text-right text-small whitespace-nowrap text-muted tabular-nums">
        {formatAgo(run.last_updated)}
      </td>
    </tr>
  )
}

interface RunRowsProps {
  filters: FetchExecutionsParams
  /** Rows to show at first. */
  initial?: number
  /** False holds the request (a placeholder shows) until what the filters depend on is known. */
  enabled?: boolean
}

/**
 * The runs behind a panel entry, in the order the API gives (newest first, or slowest first
 * with `filters.sort`, which marks the runs the p95 tile leaves out): the API's own filtering,
 * the first rows and a "Show more" for the rest. A row opens the task in the panel.
 */
export function RunRows({ filters, initial = PAGE, enabled = true }: RunRowsProps): ReactElement {
  const [view, setView] = useEntryView()
  const more = view.more ?? 0
  // The API takes at most MAX_LIMIT rows per request
  const limit = Math.min(MAX_LIMIT, initial + more * PAGE)
  const capped = limit >= MAX_LIMIT
  const query = usePanelRuns(filters, limit, { enabled })
  // While a longer list loads, `data` is the shorter one (placeholder): its failure is "load more"
  const { data } = query
  const state = useViewState(query, `panel/runs/${JSON.stringify(filters)}/${limit}`)
  if (state.phase === 'error') return <LoadFailure what="runs" view={state} />
  if (!data) return <TableSkeleton label="runs" rows={3} bare />
  if (data.tasks.length === 0) {
    return (
      <>
        <PageStale views={[state]} className="mb-2" />
        <p className="m-0 py-4 text-center text-small text-muted">No runs match.</p>
      </>
    )
  }
  return (
    <>
      <PageStale views={[state]} className="mb-2" />
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
              <RunRow key={run.task_id} run={run} sort={filters.sort} />
            ))}
          </tbody>
        </table>
      </div>
      {data.total > data.tasks.length && capped && (
        <p className="m-0 mt-2 text-small text-muted">
          Showing the first {formatCount(data.tasks.length)} of {formatCount(data.total)} runs.
        </p>
      )}
      {data.total > data.tasks.length && !capped && (
        <button
          type="button"
          onClick={() =>
            setView((now) => ({ ...now, more: Math.min(MAX_MORE, (now.more ?? 0) + 1) }))
          }
          className="mt-2 inline-flex h-[30px] items-center rounded-[3px] bg-raised px-3 text-small font-medium hover:bg-press"
        >
          Show {Math.min(PAGE, data.total - data.tasks.length, MAX_LIMIT - data.tasks.length)} more
          of {formatCount(data.total)}
        </button>
      )}
      {state.loadMoreFailed && (
        <MoreFailed error={state.error} retry={state.retry} busy={state.retrying} />
      )}
    </>
  )
}
