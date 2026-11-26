import type { TaskNode } from '@/api/client'
import { effectiveTimes, sortEventsChronologically } from '@/utils/events'

/** What a part of a run's bar shows: the wait before the first start, earlier attempts, the last run. */
export type PartKind = 'queued' | 'tries' | 'run'

export interface BarPart {
  kind: PartKind
  /** Milliseconds since the epoch. */
  from: number
  /** The latest event; for an open part the bar runs on to the axis end (see `axisEnd`). */
  to: number
  /** Still going: a started run with no end, or a wait with no start yet. */
  open?: boolean
}

/**
 * The parts of one run's bar, in time order: queue wait up to the first start, the earlier
 * attempts up to the last start, then the last run to the final event. A STARTED run's last
 * part is open. A PENDING or RECEIVED run is one open wait. A run that ended without a start is
 * one run part. Events missing: first_seen to last_updated as a run.
 */
export function barParts(task: TaskNode): BarPart[] {
  const running = task.state === 'STARTED'
  const waiting = task.state === 'PENDING' || task.state === 'RECEIVED'
  if (task.events.length === 0) {
    if (!task.first_seen || !task.last_updated) return []
    const from = Date.parse(task.first_seen)
    const to = Date.parse(task.last_updated)
    if (waiting) return [{ kind: 'queued', from, to, open: true }]
    return [{ kind: 'run', from, to, open: running }]
  }
  const sorted = sortEventsChronologically([...task.events])
  const times = effectiveTimes(sorted)
  const last = times.length - 1
  const firstStart = sorted.findIndex((event) => event.state === 'STARTED')
  if (firstStart === -1) {
    return waiting
      ? [{ kind: 'queued', from: times[0], to: times[last], open: true }]
      : [{ kind: 'run', from: times[0], to: times[last] }]
  }
  const lastStart = sorted.map((event) => event.state).lastIndexOf('STARTED')

  const parts: BarPart[] = []
  if (times[firstStart] > times[0]) {
    parts.push({ kind: 'queued', from: times[0], to: times[firstStart] })
  }
  if (lastStart > firstStart) {
    parts.push({ kind: 'tries', from: times[firstStart], to: times[lastStart] })
  }
  parts.push({ kind: 'run', from: times[lastStart], to: times[last], open: running })
  return parts
}

/** `14:05:09`, or `10-17 14:05` when the axis spans a day or more: UTC, 24-hour. */
export function utcLabel(time: number, spanMs: number): string {
  const iso = new Date(time).toISOString()
  return spanMs >= 86_400_000 ? `${iso.slice(5, 10)} ${iso.slice(11, 16)}` : iso.slice(11, 19)
}

/** The states a legend lists: those present, in the order the rest of the UI uses. */
export function legendStates(states: Iterable<string>): string[] {
  const order = ['SUCCESS', 'FAILURE', 'RETRY', 'STARTED', 'RECEIVED', 'PENDING']
  const rank = (state: string): number => {
    const index = order.indexOf(state)
    return index === -1 ? order.length : index
  }
  return [...new Set(states)].sort((a, b) => rank(a) - rank(b) || a.localeCompare(b))
}
