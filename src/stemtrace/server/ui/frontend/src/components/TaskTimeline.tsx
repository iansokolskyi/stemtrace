import { Link } from '@tanstack/react-router'
import { clsx } from 'clsx'
import { type ReactElement, type ReactNode, useMemo } from 'react'
import type { TaskNode, TimedFilters } from '@/api/client'
import { useTasks } from '@/api/queries'
import { toneOf } from '@/components/flow/flowFacts'
import { Icon } from '@/components/Icon'
import { stateLook } from '@/components/StatusChip'
import { Tooltip } from '@/components/Tooltip'
import { formatSpan } from '@/utils/format'
import { shortestUniqueSuffixes } from '@/utils/taskNames'
import { type BarPart, barParts, legendStates, utcLabel } from './timelineBars'

interface TaskTimelineProps {
  filters: TimedFilters
}

/** Fixed columns, so the grid lines and the bars line up: label, gap, plot, gap, duration. */
const COLUMNS = 'grid grid-cols-[11rem_minmax(0,1fr)_4rem] gap-x-3'
const TICKS = [0, 0.25, 0.5, 0.75, 1]
const MIN_BAR_PX = 3

interface TimelineRowData {
  task: TaskNode
  label: string
  parts: BarPart[]
}

/**
 * The Executions page's Timeline view: a row per run on one time axis (UTC, 24-hour), with the
 * same bar language as the flow page's timeline: a thin track for the wait, a hatch for
 * earlier attempts, a block in the state's colour for the last run.
 */
export function TaskTimeline({ filters }: TaskTimelineProps): ReactElement {
  const { data, isLoading, error } = useTasks(filters)

  const timed = useMemo(
    () => (data?.tasks ?? []).filter((t) => t.first_seen && t.last_updated),
    [data],
  )
  const rows = useMemo<TimelineRowData[]>(() => {
    const names = shortestUniqueSuffixes(timed.map((t) => t.name))
    return timed
      .map((task) => ({ task, label: names.get(task.name) ?? task.name, parts: barParts(task) }))
      .filter((row) => row.parts.length > 0)
  }, [timed])

  if (isLoading) return <TimelineSkeleton />
  if (error) return <Notice tone="error">Failed to load timeline</Notice>
  if (!data?.tasks.length) return <Notice>No executions to display</Notice>
  if (rows.length === 0) return <Notice>No timing data available</Notice>

  const times = rows.flatMap((row) => row.parts.flatMap((part) => [part.from, part.to]))
  const start = Math.min(...times)
  const end = Math.max(...times)
  const span = end - start
  const pct = (time: number): number => (span > 0 ? ((time - start) / span) * 100 : 0)
  const states = legendStates(rows.map((row) => row.task.state))

  return (
    <section aria-label="Timeline" className="min-w-0 overflow-x-auto rounded-lg bg-surface">
      <div className="min-w-[720px] px-4 pt-3 pb-3.5">
        <div className={clsx(COLUMNS, 'mb-1 h-5 text-xs text-muted')} aria-hidden="true">
          <span>UTC</span>
          <div className="relative" data-testid="timeline-axis">
            {TICKS.map((tick) => (
              <span
                key={tick}
                className={clsx(
                  'absolute top-0 whitespace-nowrap tabular-nums',
                  tick === 1 && '-translate-x-full',
                  tick > 0 && tick < 1 && '-translate-x-1/2',
                )}
                style={{ left: `${tick * 100}%` }}
              >
                {utcLabel(start + span * tick, span)}
              </span>
            ))}
          </div>
        </div>

        <div className="relative">
          <div
            aria-hidden="true"
            className="pointer-events-none absolute inset-y-0 right-[5.75rem] left-[12.75rem]"
          >
            {TICKS.map((tick) => (
              <div
                key={tick}
                className="absolute inset-y-0 w-px bg-rule"
                style={{ left: `${tick * 100}%` }}
              />
            ))}
          </div>
          {rows.map((row) => (
            <TimelineRow key={row.task.task_id} row={row} pct={pct} />
          ))}
        </div>

        <ul className="m-0 mt-3 flex list-none flex-wrap gap-x-4 gap-y-1 p-0 pt-3 text-xs text-muted shadow-[0_-1px_0_var(--rule)]">
          {states.map((state) => (
            <li
              key={state}
              data-testid="timeline-legend-item"
              className="flex items-center gap-1.5"
            >
              <span
                aria-hidden="true"
                className={clsx(
                  'size-3 rounded-[3px]',
                  state === 'RETRY' ? 'retry-hatch' : `s-${toneOf(state)} bg-[var(--c)]`,
                )}
              />
              {stateLook(state).word}
            </li>
          ))}
        </ul>
      </div>
    </section>
  )
}

interface TimelineRowProps {
  row: TimelineRowData
  pct: (time: number) => number
}

function TimelineRow({ row, pct }: TimelineRowProps): ReactElement {
  const { task, label, parts } = row
  const look = stateLook(task.state)
  const tone = toneOf(task.state)
  const placement = pct(parts[0].from) > 60 ? 'bottom-end' : 'bottom'

  return (
    <div
      data-testid="timeline-row"
      data-state={task.state}
      className={clsx(COLUMNS, 'relative h-6 items-center rounded-[3px] hover:bg-hover')}
    >
      <Link
        to="/tasks/$taskId"
        params={{ taskId: task.task_id }}
        className="flex min-w-0 items-center gap-2 text-xs text-ink hover:underline"
        title={task.name}
        aria-label={task.name}
      >
        <span
          aria-hidden="true"
          className={clsx(
            's-' + tone,
            'flex size-4 flex-none items-center justify-center rounded-[3px] bg-[var(--c)] text-[var(--on)]',
          )}
        >
          <Icon name={look.icon} size={12} />
        </span>
        <span className="truncate">{label}</span>
      </Link>

      <div className="relative h-6">
        <div className="absolute inset-x-0 top-[11px] h-0.5 bg-rule" aria-hidden="true" />
        {parts.map((part) => {
          const left = pct(part.from)
          const width = Math.max(pct(part.to) - left, 0)
          const words =
            part.kind === 'queued'
              ? 'Queued'
              : part.kind === 'tries'
                ? 'Earlier attempts'
                : look.word
          return (
            <div
              key={part.kind}
              data-part={part.kind}
              className="absolute"
              style={{
                left: `${left}%`,
                width: `max(${width}%, ${MIN_BAR_PX}px)`,
                top: part.kind === 'queued' ? 9 : 5,
                height: part.kind === 'queued' ? 6 : 14,
              }}
            >
              <Tooltip
                className="h-full"
                placement={placement}
                content={`${words} · ${formatSpan(part.to - part.from)} · ${utcLabel(part.from, 0)} UTC`}
              >
                <div
                  data-fill=""
                  className={clsx(
                    'h-full rounded-[3px]',
                    part.kind === 'queued' && 'bg-track',
                    part.kind === 'tries' && 'retry-hatch',
                    part.kind === 'run' &&
                      (task.state === 'RETRY' ? 'retry-hatch' : `s-${tone} bg-[var(--c)]`),
                  )}
                />
              </Tooltip>
            </div>
          )
        })}
      </div>

      <div className="text-right text-xs text-muted tabular-nums">
        {task.duration_ms !== null ? formatSpan(task.duration_ms) : '–'}
      </div>
    </div>
  )
}

function Notice({ children, tone }: { children: ReactNode; tone?: 'error' }): ReactElement {
  return (
    <div
      className={clsx(
        'rounded-lg bg-surface px-4 py-10 text-center text-xs',
        tone === 'error' ? 'font-semibold text-failure-t' : 'text-muted',
      )}
    >
      {children}
    </div>
  )
}

/** The view's own shape while the runs load: five rows, no motion. */
function TimelineSkeleton(): ReactElement {
  return (
    <div aria-hidden="true" className="rounded-lg bg-surface px-4 py-3.5">
      <div className="mb-3 h-4 rounded-[3px] bg-raised" />
      {[0, 1, 2, 3, 4].map((i) => (
        <div key={i} className={clsx(COLUMNS, 'h-6 items-center')}>
          <div className="h-3 rounded-[3px] bg-raised" />
          <div className="h-3 rounded-[3px] bg-raised" />
          <div className="h-3 rounded-[3px] bg-raised" />
        </div>
      ))}
    </div>
  )
}
