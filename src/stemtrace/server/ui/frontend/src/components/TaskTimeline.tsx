import { Link } from '@tanstack/react-router'
import { clsx } from 'clsx'
import { type ReactElement, useEffect, useMemo, useState } from 'react'
import type { TaskNode, TimedFilters } from '@/api/client'
import { useTasks } from '@/api/queries'
import { toneOf } from '@/components/flow/flowFacts'
import { axisEnd } from '@/components/flow/timeline'
import { Icon } from '@/components/Icon'
import { LoadFailure, Notice } from '@/components/Notice'
import { PageStale } from '@/components/PageStale'
import { Skeleton, SkeletonRegion } from '@/components/Skeleton'
import { stateLook } from '@/components/StatusChip'
import { Tooltip } from '@/components/Tooltip'
import { useViewState } from '@/hooks/useViewState'
import { formatSpan } from '@/utils/format'
import { shortestUniqueSuffixes } from '@/utils/taskNames'
import { type BarPart, barParts, legendStates, utcLabel } from './timelineBars'

interface TaskTimelineProps {
  filters: TimedFilters
}

/** One column template for the axis, the grid lines and every row, so they line up. */
const COLUMNS = 'grid grid-cols-[11rem_minmax(0,1fr)_4rem] gap-x-3'
const TICKS = [0, 0.25, 0.5, 0.75, 1]
const MIN_BAR_PX = 3

interface TimelineRowData {
  task: TaskNode
  label: string
  parts: BarPart[]
}

/** What the rows are drawn against: the axis, and the clock open runs are measured to. */
interface Scale {
  start: number
  end: number
  /** An open run has run past the axis end (the cap in `axisEnd`): its bar stops at a cut mark. */
  cut: boolean
  now: number
}

/**
 * The Executions page's Timeline view: a row per run on one time axis (UTC, 24-hour), with the
 * same bar language as the flow page's timeline: a thin track for the wait, a hatch for
 * earlier attempts, a block in the state's colour for the last run. Open runs follow the flow
 * page's rule (`axisEnd`): they run to now.
 */
export function TaskTimeline({ filters }: TaskTimelineProps): ReactElement {
  const query = useTasks(filters)
  const { data } = query
  const view = useViewState(query, `tasks/timeline/${JSON.stringify(filters)}`)

  const rows = useMemo<TimelineRowData[]>(() => {
    const timed = (data?.tasks ?? []).filter((t) => t.first_seen && t.last_updated)
    const names = shortestUniqueSuffixes(timed.map((t) => t.name))
    return timed.map((task) => ({
      task,
      label: names.get(task.name) ?? task.name,
      parts: barParts(task),
    }))
  }, [data])

  // Open runs run to now: the clock ticks once a second while one is open and stops otherwise
  const anyOpen = rows.some((row) => row.parts.some((part) => part.open))
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (!anyOpen) return
    setNow(Date.now())
    const timer = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(timer)
  }, [anyOpen])

  if (view.phase === 'loading') return <TimelineSkeleton />
  if (view.phase === 'error') return <LoadFailure what="the timeline" view={view} />
  if (!data?.tasks.length) return <Notice>No executions to display</Notice>
  if (rows.length === 0) return <Notice>No timing data available</Notice>

  const parts = rows.flatMap((row) => row.parts)
  const start = Math.min(...parts.map((part) => part.from))
  const settledEnd = Math.max(...parts.map((part) => (part.open ? part.from : part.to)))
  const { t1, cut } = axisEnd(start, settledEnd, anyOpen, now)
  const scale: Scale = { start, end: t1, cut, now }
  const span = t1 - start
  // Every row's state, as its mark shows; a hatch is Retry, also for an earlier attempt alone
  const hasTries = parts.some((part) => part.kind === 'tries')
  const states = legendStates([...rows.map((r) => r.task.state), ...(hasTries ? ['RETRY'] : [])])

  return (
    <>
      <PageStale views={[view]} className="mb-3" />
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
              className={clsx(COLUMNS, 'pointer-events-none absolute inset-0')}
            >
              <span />
              <div className="relative">
                {TICKS.map((tick) => (
                  <div
                    key={tick}
                    data-testid="timeline-gridline"
                    className="absolute inset-y-0 w-px bg-rule"
                    style={{ left: `${tick * 100}%` }}
                  />
                ))}
              </div>
            </div>
            {rows.map((row) => (
              <TimelineRow key={row.task.task_id} row={row} scale={scale} />
            ))}
          </div>

          <ul className="m-0 mt-3 flex list-none flex-wrap gap-x-4 gap-y-1 p-0 pt-3 text-xs text-muted shadow-[0_-1px_0_var(--rule)]">
            {states.map((state) => (
              <LegendItem
                key={state}
                word={stateLook(state).word}
                swatch={state === 'RETRY' ? 'retry-hatch' : `s-${toneOf(state)} bg-[var(--c)]`}
              />
            ))}
            {parts.some((part) => part.kind === 'queued') && (
              <LegendItem word="Waiting" swatch="bg-track" />
            )}
          </ul>
        </div>
      </section>
    </>
  )
}

function LegendItem({ word, swatch }: { word: string; swatch: string }): ReactElement {
  return (
    <li data-testid="timeline-legend-item" className="flex items-center gap-1.5">
      <span aria-hidden="true" className={clsx('size-3 rounded-[3px]', swatch)} />
      {word}
    </li>
  )
}

interface TimelineRowProps {
  row: TimelineRowData
  scale: Scale
}

function TimelineRow({ row, scale }: TimelineRowProps): ReactElement {
  const { task, label, parts } = row
  const look = stateLook(task.state)
  const tone = toneOf(task.state)
  const span = scale.end - scale.start
  const pct = (time: number): number => ((time - scale.start) / span) * 100
  const placement = pct(parts[0].from) > 60 ? 'bottom-end' : 'bottom'
  const openPart = parts.find((part) => part.open)
  // Held at the latest event, so a clock behind the server never reads as a negative time
  const elapsed = openPart ? Math.max(scale.now, openPart.to) - openPart.from : null
  // A clock behind the run's start gives no time to show: the words alone
  const hasElapsed = elapsed !== null && elapsed > 0
  const lastIndex = parts.length - 1

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
        aria-label={`${task.name}, ${look.word}`}
      >
        <span
          aria-hidden="true"
          className={clsx(
            `s-${tone}`,
            'flex size-4 flex-none items-center justify-center rounded-[3px] bg-[var(--c)] text-[var(--on)]',
          )}
        >
          <Icon name={look.icon} size={12} />
        </span>
        <span className="truncate">{label}</span>
      </Link>

      <div className="relative h-6">
        <div className="absolute inset-x-0 top-[11px] h-0.5 bg-rule" aria-hidden="true" />
        {parts.map((part, index) => {
          const width = (Math.max((part.open ? scale.end : part.to) - part.from, 0) / span) * 100
          const words =
            part.kind === 'queued'
              ? 'Waiting'
              : part.kind === 'tries'
                ? 'Earlier attempt'
                : look.word
          const time = `${utcLabel(part.from, span)} UTC`
          const content = part.open
            ? `${words}${hasElapsed ? ` for ${formatSpan(elapsed)}` : ''} · ${time}`
            : `${words} · ${formatSpan(part.to - part.from)} · ${time}`
          return (
            <div
              key={part.kind}
              data-part={part.kind}
              className="absolute"
              style={{
                left: `${pct(part.from)}%`,
                width: `max(${width}%, ${MIN_BAR_PX}px)`,
                top: part.kind === 'queued' ? 9 : 5,
                height: part.kind === 'queued' ? 6 : 14,
              }}
            >
              <Tooltip className="h-full" placement={placement} content={content}>
                {/* One focus stop per row (its last part), so the tooltip shows from the keyboard
                    without a tab stop for every bar */}
                <div
                  data-fill=""
                  role="img"
                  aria-label={content}
                  tabIndex={index === lastIndex ? 0 : undefined}
                  className={clsx(
                    'h-full rounded-[3px]',
                    part.kind === 'queued' && 'bg-track',
                    part.kind === 'tries' && 'retry-hatch',
                    part.kind === 'run' &&
                      (task.state === 'RETRY' ? 'retry-hatch' : `s-${tone} bg-[var(--c)]`),
                  )}
                />
              </Tooltip>
              {part.open && scale.cut && (
                <span
                  data-cut=""
                  aria-hidden="true"
                  className="absolute -top-0.5 -right-px h-[18px] w-0.5 rotate-[20deg] bg-ink"
                />
              )}
            </div>
          )
        })}
      </div>

      <div data-testid="timeline-duration" className="text-right text-xs text-muted tabular-nums">
        {openPart
          ? hasElapsed
            ? formatSpan(elapsed)
            : openPart.kind === 'queued'
              ? 'Waiting'
              : look.word
          : task.duration_ms !== null
            ? formatSpan(task.duration_ms)
            : '–'}
      </div>
    </div>
  )
}

/** The view's own shape while the runs load: an axis row and five run rows. */
function TimelineSkeleton(): ReactElement {
  return (
    <SkeletonRegion label="timeline" className="rounded-lg bg-surface px-4 py-3.5">
      <Skeleton className="mb-3 h-4" />
      {[0, 1, 2, 3, 4].map((i) => (
        <div key={i} className={clsx(COLUMNS, 'h-6 items-center')}>
          <Skeleton className="h-3" />
          <Skeleton className="h-3" />
          <Skeleton className="h-3" />
        </div>
      ))}
    </SkeletonRegion>
  )
}
