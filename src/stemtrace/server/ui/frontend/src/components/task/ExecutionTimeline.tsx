import { clsx } from 'clsx'
import { type ReactElement, useMemo, useState } from 'react'
import type { TaskEvent } from '@/api/client'
import { buildSegments, effectiveTimes, sortEventsChronologically } from '@/components/TimelineBar'
import { formatSpan, formatTime } from '@/utils/format'
import { Panel } from './Panel'
import { stateFill } from './stateFill'

/**
 * One bar for the whole run, a segment per event sized by how long the task stayed in that
 * state, and the same segments listed below with their durations. Nothing for fewer than two events.
 */
export function ExecutionTimeline({ events }: { events: TaskEvent[] }): ReactElement | null {
  const [hovered, setHovered] = useState<number | null>(null)
  const segments = useMemo(() => buildSegments(events), [events])
  if (events.length < 2 || segments.length === 0) return null

  const times = effectiveTimes(sortEventsChronologically(events))
  const iso = (time: number): string => new Date(time).toISOString()

  return (
    <Panel title="Execution Timeline">
      <div className="px-4 pt-2 pb-3.5">
        <div
          aria-hidden="true"
          className="flex h-6 gap-0.5 overflow-hidden rounded-[3px] bg-raised"
        >
          {segments.map((segment, i) => (
            <div
              // Segments are positional: the same events always give the same list
              key={i}
              className={clsx('h-full', stateFill(segment.state))}
              style={
                segment.isTerminal
                  ? { flex: '0 0 8px' }
                  : { flex: `${Math.max(segment.duration, 1)} 1 0`, minWidth: '6px' }
              }
            />
          ))}
        </div>
        <div className="mt-1.5 flex justify-between text-xs text-muted">
          <span>{formatTime(iso(times[0]))}</span>
          <span>{formatTime(iso(times[times.length - 1]))}</span>
        </div>
        <div className="mt-3 flex flex-wrap gap-x-5 gap-y-2 text-xs">
          {segments.map((segment, i) => (
            <div
              key={i}
              data-testid="timeline-segment"
              role="presentation"
              className={clsx(
                'flex items-center gap-2 transition-opacity',
                hovered !== null && hovered !== i && 'opacity-50',
              )}
              onMouseEnter={() => setHovered(i)}
              onMouseLeave={() => setHovered(null)}
            >
              <span
                data-segment-swatch
                className={clsx(
                  'h-3.5 rounded-[3px]',
                  segment.isTerminal ? 'w-2' : 'w-3.5',
                  stateFill(segment.state),
                )}
              />
              <span className="text-muted">{segment.state}</span>
              <span className="font-bold">{formatSpan(segment.duration)}</span>
            </div>
          ))}
        </div>
      </div>
    </Panel>
  )
}
