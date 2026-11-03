import { clsx } from 'clsx'
import { Fragment, type ReactElement, useState } from 'react'
import type {
  GroupStats,
  GroupTimeseriesResponse,
  TaskStats,
  TaskStatsResponse,
} from '@/api/client'
import { Icon } from '@/components/Icon'
import { TaskName } from '@/components/TaskName'
import { openerProps, usePanel } from '@/components/triage/PanelContext'
import { encodeScope } from '@/components/triage/runsScope'
import { formatAgo, formatCount, formatPercent, formatSpan } from '@/utils/format'
import { Sparkline } from './Sparkline'

/** A failure rate above this tints the row and makes the figure loud (the prototype's `prob`). */
const PROBLEM_RATE = 0.01

const TH =
  'h-9 whitespace-nowrap border-b border-rule bg-surface px-2.5 text-left text-xs font-semibold text-muted first:pl-4 last:pr-4'
const TD = 'whitespace-nowrap px-2.5 py-1 text-xs first:pl-4 last:pr-4'
const NUM = 'text-right tabular-nums'

const ms = (value: number | null | undefined): string => (value == null ? '' : formatSpan(value))

interface HealthTableProps {
  stats: TaskStatsResponse | undefined
  /** Runs per bucket by state for each group; none for All or when the server cannot split. */
  series: GroupTimeseriesResponse | undefined
  within: string | undefined
  windowWords: string | undefined
}

function groupSpark(series: GroupTimeseriesResponse | undefined, group: string): number[] {
  const byState = series?.by_task_group?.[group]
  if (!byState) return []
  const outcomes = ['SUCCESS', 'FAILURE', 'REVOKED', 'REJECTED'].map((s) => byState[s] ?? [])
  const length = Math.max(0, ...outcomes.map((o) => o.length))
  return Array.from({ length }, (_, i) => outcomes.reduce((sum, o) => sum + (o[i] ?? 0), 0))
}

/**
 * Task health by group: runs, failure rate, runtimes and a trend per group, each group opening
 * in the triage panel and expanding to its tasks (a task opens its runs). Problem rows are
 * tinted and carry an icon, so colour is never the only signal.
 */
export function HealthTable({
  stats,
  series,
  within,
  windowWords,
}: HealthTableProps): ReactElement {
  const panel = usePanel()
  const [closed, setClosed] = useState<ReadonlySet<string>>(new Set())

  const groups = [...(stats?.groups ?? [])]
    .filter((g) => g.runs > 0)
    .sort((a, b) => (b.failure_rate ?? 0) - (a.failure_rate ?? 0) || b.runs - a.runs)
  const tasksOf = (group: GroupStats): TaskStats[] =>
    (stats?.tasks ?? [])
      .filter((t) => t.task_group === group.name && t.runs > 0)
      .sort((a, b) => (b.failure_rate ?? 0) - (a.failure_rate ?? 0) || b.runs - a.runs)

  const toggle = (name: string) =>
    setClosed((prev) => {
      const next = new Set(prev)
      if (!next.delete(name)) next.add(name)
      return next
    })

  return (
    <section aria-labelledby="h-health" className="mb-4 min-w-0 rounded-lg bg-surface">
      <div className="flex flex-wrap items-center gap-x-3.5 gap-y-1 px-4 pt-3.5 pb-2">
        <h2 id="h-health" className="text-sm font-bold">
          Task health by group
        </h2>
        <span className="min-h-4 text-xs text-muted">{windowWords ?? 'all runs held'}</span>
      </div>
      {stats && groups.length === 0 ? (
        <p className="m-0 px-4 pt-2 pb-4 text-xs text-muted">
          No runs{windowWords ? ` in ${windowWords}` : ''}.
        </p>
      ) : (
        <div className="overflow-x-auto">
          <table aria-label="Task health by group" className="w-full min-w-[640px] border-collapse">
            <thead>
              <tr>
                <th scope="col" className={TH}>
                  Group and task
                </th>
                <th scope="col" className={clsx(TH, NUM)}>
                  Runs
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
                  Trend
                </th>
                <th scope="col" className={clsx(TH, NUM)}>
                  Last run
                </th>
              </tr>
            </thead>
            <tbody>
              {groups.map((group) => {
                const isClosed = closed.has(group.name)
                const spark = groupSpark(series, group.name)
                const problem = (group.failure_rate ?? 0) > PROBLEM_RATE
                return (
                  <Fragment key={group.name}>
                    <tr
                      data-testid="health-group"
                      className="relative bg-raised font-bold has-[[data-on]]:bg-[color-mix(in_srgb,var(--ink)_10%,var(--surface))]"
                    >
                      <td className={TD}>
                        <button
                          type="button"
                          aria-expanded={!isClosed}
                          aria-label={`${isClosed ? 'Expand' : 'Collapse'} ${group.name}`}
                          onClick={() => toggle(group.name)}
                          className="relative z-1 mr-1 inline-flex size-[18px] items-center justify-center rounded-[2px] text-muted hover:bg-hover hover:text-ink"
                        >
                          <Icon name="chev" size={12} className={isClosed ? '' : 'rotate-90'} />
                        </button>
                        <button
                          type="button"
                          {...openerProps(panel, { kind: 'group', id: group.name })}
                          className="after:absolute after:inset-0"
                        >
                          {group.name}
                        </button>
                      </td>
                      <td className={clsx(TD, NUM)}>{formatCount(group.runs)}</td>
                      <td className={clsx(TD, NUM, problem && 'text-failure-t')}>
                        {group.failure_rate == null ? '' : formatPercent(group.failure_rate)}
                      </td>
                      <td className={clsx(TD, NUM)} />
                      <td className={clsx(TD, NUM)}>{ms(group.p95_ms)}</td>
                      <td className={clsx(TD, NUM)}>
                        {spark.length > 1 && (
                          <span className="inline-block align-middle">
                            <Sparkline
                              values={spark}
                              width={84}
                              height={18}
                              grade={problem ? 'bad' : 'ok'}
                            />
                          </span>
                        )}
                      </td>
                      <td className={clsx(TD, NUM, 'font-normal text-muted')}>
                        {formatAgo(group.last_run)}
                      </td>
                    </tr>
                    {!isClosed &&
                      tasksOf(group).map((task) => {
                        const bad = (task.failure_rate ?? 0) > PROBLEM_RATE
                        return (
                          <tr
                            key={task.name}
                            data-testid="health-task"
                            className={clsx(
                              'relative border-b border-rule last:border-b-0 has-[[data-on]]:bg-[color-mix(in_srgb,var(--ink)_10%,var(--surface))]',
                              bad
                                ? 'bg-[color-mix(in_srgb,var(--failure)_9%,var(--surface))] hover:bg-[color-mix(in_srgb,var(--failure)_15%,var(--surface))]'
                                : 'hover:bg-hover',
                            )}
                          >
                            <td className={clsx(TD, 'pl-[34px]')}>
                              <button
                                type="button"
                                {...openerProps(panel, {
                                  kind: 'runs',
                                  id: encodeScope({ kind: 'name', name: task.name, within }),
                                })}
                                className="max-w-[360px] text-left after:absolute after:inset-0"
                              >
                                <TaskName name={task.name} />
                              </button>
                            </td>
                            <td className={clsx(TD, NUM)}>{formatCount(task.runs)}</td>
                            <td className={clsx(TD, NUM, bad && 'font-bold text-failure-t')}>
                              {bad && (
                                <Icon name="x" size={12} className="mr-1 inline align-[-1px]" />
                              )}
                              {task.failure_rate == null ? '' : formatPercent(task.failure_rate)}
                            </td>
                            <td className={clsx(TD, NUM)}>{ms(task.p50_ms)}</td>
                            <td className={clsx(TD, NUM)}>{ms(task.p95_ms)}</td>
                            <td className={clsx(TD, NUM)} />
                            <td className={clsx(TD, NUM, 'text-muted')}>
                              {formatAgo(task.last_run ?? null)}
                            </td>
                          </tr>
                        )
                      })}
                  </Fragment>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
    </section>
  )
}
