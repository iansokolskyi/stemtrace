import type { TaskNode } from '@/api/client'
import { effectiveTimes, sortEventsChronologically } from '@/utils/events'

/** What a part of a run's bar shows: the wait before the first start, earlier attempts, the last run. */
export type PartKind = 'queued' | 'tries' | 'run'

export interface BarPart {
  kind: PartKind
  /** Milliseconds since the epoch. */
  from: number
  to: number
}

/**
 * The parts of one run's bar, in time order: queue wait up to the first start, the earlier
 * attempts up to the last start, then the last run to the final event. A run that never started
 * is one run part, so it still has a bar. Events missing: first_seen to last_updated as a run.
 */
export function barParts(task: TaskNode): BarPart[] {
  if (task.events.length === 0) {
    if (!task.first_seen || !task.last_updated) return []
    return [{ kind: 'run', from: Date.parse(task.first_seen), to: Date.parse(task.last_updated) }]
  }
  const sorted = sortEventsChronologically([...task.events])
  const times = effectiveTimes(sorted)
  const last = times.length - 1
  const firstStart = sorted.findIndex((event) => event.state === 'STARTED')
  if (firstStart === -1) return [{ kind: 'run', from: times[0], to: times[last] }]
  const lastStart = sorted.map((event) => event.state).lastIndexOf('STARTED')

  const parts: BarPart[] = []
  if (times[firstStart] > times[0]) {
    parts.push({ kind: 'queued', from: times[0], to: times[firstStart] })
  }
  if (lastStart > firstStart) {
    parts.push({ kind: 'tries', from: times[firstStart], to: times[lastStart] })
  }
  parts.push({ kind: 'run', from: times[lastStart], to: times[last] })
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
