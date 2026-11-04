import { createFileRoute, Link } from '@tanstack/react-router'
import { clsx } from 'clsx'
import { useState } from 'react'
import { useRegistryPage } from '@/api/queries'
import { Icon } from '@/components/Icon'
import { RegistryStatus } from '@/components/registry/RegistryStatus'
import { matchesQuery, noteOf } from '@/components/registry/registry'
import { TableSkeleton } from '@/components/TableSkeleton'
import { TaskName } from '@/components/TaskName'
import { NUM, TD, TH } from '@/components/workers/workers'
import { useCarriedRange } from '@/hooks/useCarriedRange'
import { formatAgo, formatCount, formatPercent, formatSpan } from '@/utils/format'

export const Route = createFileRoute('/registry')({
  component: RegistryPage,
  staticData: { crumb: 'Registry' },
})

// A not-registered row is tinted like the prototype's: anomaly at 9%, 15% under the pointer.
const PROBLEM_ROW =
  'bg-[color-mix(in_srgb,var(--anomaly)_9%,var(--surface))] hover:bg-[color-mix(in_srgb,var(--anomaly)_15%,var(--surface))]'

/** The window the stats columns cover, and the label that says so. */
const STATS_WITHIN = '6h'

/** A failure rate above this is shown loud, as the prototype does. */
const HIGH_FAILURE_RATE = 0.01

/** A runtime cell: empty while stats are unknown, a dash with no samples (the prototype's). */
function spanCell(statsKnown: boolean, ms: number | null | undefined): string {
  if (!statsKnown) return ''
  return ms == null ? '—' : formatSpan(ms)
}

function RegistryPage() {
  const { data: page, isLoading, error } = useRegistryPage(STATS_WITHIN)
  const data = page?.registry
  const stats = page?.stats
  // Joined by exact name. While loading, or when the request fails, no cell shows a number.
  const statsKnown = stats != null
  const statsByName = new Map(stats?.tasks.map((row) => [row.name, row]))
  const carried = useCarriedRange()
  const [query, setQuery] = useState('')
  const tasks = data?.tasks ?? []
  const shown = tasks.filter((task) => matchesQuery(task, query))
  // A group column only when some task has a real group: older servers send none, and a default
  // install sends "ungrouped" on every row
  const showGroup = tasks.some((task) => task.task_group && task.task_group !== 'ungrouped')

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-2xl font-bold text-ink">Registry</h1>
        <p className="mt-1 min-h-5 text-sm text-muted">
          {data &&
            `${formatCount(tasks.length)} task ${tasks.length === 1 ? 'name' : 'names'} known to workers or seen on the broker`}
        </p>
      </div>

      {tasks.length > 0 && (
        <div className="relative max-w-[360px]">
          <label htmlFor="registry-search" className="sr-only">
            Search the registry
          </label>
          <input
            id="registry-search"
            type="search"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Search task name, group or status"
            autoComplete="off"
            className="h-8 w-full rounded-[3px] border-0 bg-raised pr-8 pl-2.5 text-xs text-ink placeholder:text-muted"
          />
          <Icon
            name="search"
            size={14}
            className="pointer-events-none absolute top-1/2 right-2.5 -translate-y-1/2 text-muted"
          />
        </div>
      )}

      {isLoading ? (
        <TableSkeleton />
      ) : error ? (
        <p className="rounded-lg bg-surface px-4 py-10 text-center text-xs font-semibold text-failure-t">
          Failed to load task registry
        </p>
      ) : tasks.length === 0 ? (
        <div className="rounded-lg bg-surface px-4 py-10 text-center text-xs text-muted">
          <p className="m-0">No tasks discovered yet</p>
          <p className="m-0 mt-1">Run some Celery tasks to see them appear here.</p>
        </div>
      ) : shown.length === 0 ? (
        <p className="rounded-lg bg-surface px-4 py-10 text-center text-xs text-muted">
          No tasks match “{query.trim()}”.
        </p>
      ) : (
        <div className="overflow-x-auto rounded-lg bg-surface">
          <table aria-label="Registry" className="w-full min-w-[820px] border-collapse">
            <thead>
              <tr>
                <th scope="col" className={TH}>
                  Task
                </th>
                {showGroup && (
                  <th scope="col" className={TH}>
                    Group
                  </th>
                )}
                <th scope="col" className={TH}>
                  Status
                </th>
                <th scope="col" className={clsx(TH, NUM)}>
                  Runs (6 h)
                </th>
                <th scope="col" className={clsx(TH, NUM)}>
                  Fail %
                </th>
                <th scope="col" className={clsx(TH, NUM)}>
                  p50
                </th>
                <th scope="col" className={clsx(TH, NUM)}>
                  p95
                </th>
                <th scope="col" className={clsx(TH, NUM)}>
                  Last run
                </th>
              </tr>
            </thead>
            <tbody>
              {shown.map((task) => {
                const problem = task.status === 'not_registered'
                const note = noteOf(task)
                // A loaded answer with no row for a name means it has no records, so no runs
                const row = statsByName.get(task.name)
                const runs = statsKnown ? (row?.runs ?? 0) : null
                const rate = row?.failure_rate ?? null
                return (
                  <tr
                    key={task.name}
                    data-problem={problem || undefined}
                    className={clsx(
                      'relative border-b border-rule last:border-b-0',
                      problem ? PROBLEM_ROW : 'hover:bg-hover',
                    )}
                  >
                    <td className="min-w-0 px-3 py-1.5 text-xs first:pl-4 last:pr-4">
                      <Link
                        to="/executions"
                        search={{ name_exact: task.name, ...carried }}
                        aria-label={task.name}
                        className="block max-w-[46ch] after:absolute after:inset-0"
                      >
                        <TaskName name={task.name} />
                      </Link>
                      {note && (
                        <p
                          data-testid="registry-note"
                          className="m-0 mt-0.5 max-w-[420px] text-xs text-muted"
                        >
                          {note}
                        </p>
                      )}
                    </td>
                    {showGroup && <td className={TD}>{task.task_group}</td>}
                    <td className={TD}>
                      <RegistryStatus status={task.status} />
                    </td>
                    <td className={clsx(TD, NUM)}>{runs === null ? '' : formatCount(runs)}</td>
                    <td
                      className={clsx(
                        TD,
                        NUM,
                        rate !== null && rate > HIGH_FAILURE_RATE && 'font-bold text-failure-t',
                      )}
                    >
                      {rate === null ? '' : formatPercent(rate)}
                    </td>
                    <td className={clsx(TD, NUM)}>{spanCell(statsKnown, row?.p50_ms)}</td>
                    <td className={clsx(TD, NUM)}>{spanCell(statsKnown, row?.p95_ms)}</td>
                    <td className={clsx(TD, NUM, 'text-muted')}>
                      {task.last_run ? formatAgo(task.last_run) : 'Never'}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
