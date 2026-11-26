import type { TaskEvent } from '@/api/client'

export const TERMINAL_STATES = new Set(['SUCCESS', 'FAILURE', 'REVOKED', 'REJECTED', 'IGNORED'])

// Fixed state order for exact timestamp ties. Matches the server's
// _STATE_ORDER (stemtrace/core/attribution.py), so the UI agrees with the API order.
const STATE_PRIORITY: Record<string, number> = {
  PENDING: 0,
  RECEIVED: 1,
  STARTED: 2,
  RETRY: 3,
  SUCCESS: 4,
  FAILURE: 5,
  REJECTED: 6,
  REVOKED: 7,
  IGNORED: 8,
}

export interface TimelineSegment {
  state: string
  duration: number
  timestamp: Date
  isTerminal: boolean
  widthPct: number
}

/**
 * Attempt-0 PENDING/RECEIVED belong before the first run whatever their
 * timestamps (PENDING first): the sender's PENDING (after_task_publish fires after
 * publish) can be stamped after the worker received, started or finished it.
 */
function isPreStart(event: TaskEvent): boolean {
  return (event.retries ?? 0) === 0 && (event.state === 'PENDING' || event.state === 'RECEIVED')
}

/**
 * Order events for display, mirroring the API's order: attempt-0 PENDING
 * then RECEIVED first, then everything by timestamp, with a fixed state
 * order for exact ties (the sort is stable). Keeps only the first PENDING and
 * RECEIVED (retry re-queues create duplicates).
 *
 * Don't sort by retry count: Celery reports failures raised in the worker main
 * process (hard time limit, lost worker) with retries=0, which would put the
 * final FAILURE before later attempts and produce negative durations.
 */
export function sortEventsChronologically(events: TaskEvent[]): TaskEvent[] {
  let hasPending = false
  let hasReceived = false
  const filtered = events.filter((e) => {
    if (e.state === 'PENDING') {
      if (hasPending) return false
      hasPending = true
    }
    if (e.state === 'RECEIVED') {
      if (hasReceived) return false
      hasReceived = true
    }
    return true
  })

  return filtered.sort((a, b) => {
    const aPreStart = isPreStart(a)
    const groupDiff = (aPreStart ? 0 : 1) - (isPreStart(b) ? 0 : 1)
    if (groupDiff !== 0) return groupDiff
    if (aPreStart) {
      // Both pre-start: PENDING before RECEIVED, whatever their timestamps.
      const stateDiff = (STATE_PRIORITY[a.state] ?? 8) - (STATE_PRIORITY[b.state] ?? 8)
      if (stateDiff !== 0) return stateDiff
    }
    const timeDiff = new Date(a.timestamp).getTime() - new Date(b.timestamp).getTime()
    if (timeDiff !== 0) return timeDiff
    return (STATE_PRIORITY[a.state] ?? 8) - (STATE_PRIORITY[b.state] ?? 8)
  })
}

/**
 * Display times for events in display order, never decreasing: a late-stamped
 * PENDING is clamped to the time of the first run (its timestamp isn't changed).
 */
export function effectiveTimes(sorted: TaskEvent[]): number[] {
  const times = sorted.map((e) => new Date(e.timestamp).getTime())
  for (let i = times.length - 2; i >= 0; i--) {
    times[i] = Math.min(times[i], times[i + 1])
  }
  return times
}

export function buildSegments(events: TaskEvent[]): TimelineSegment[] {
  if (!events || events.length === 0) return []

  const sorted = sortEventsChronologically(events)
  const times = effectiveTimes(sorted)

  const minTime = times[0]
  const maxTime = times[times.length - 1]
  const range = maxTime - minTime || 1

  const segments: TimelineSegment[] = []

  for (let i = 0; i < sorted.length; i++) {
    const event = sorted[i]
    const eventTime = times[i]
    const isTerminal = TERMINAL_STATES.has(event.state)
    const endTime = i + 1 < times.length ? times[i + 1] : eventTime

    const duration = endTime - eventTime
    const widthPct = range > 0 ? (duration / range) * 100 : 100 / sorted.length

    segments.push({
      state: event.state,
      duration,
      timestamp: new Date(event.timestamp),
      isTerminal,
      widthPct,
    })
  }

  return segments
}
