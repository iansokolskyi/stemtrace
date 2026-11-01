import { type ReactElement, useState } from 'react'
import type { TaskEvent } from '@/api/client'
import { StatusChip } from '@/components/StatusChip'
import { sortEventsChronologically } from '@/components/TimelineBar'
import { formatTime } from '@/utils/format'
import { Panel } from './Panel'

/**
 * A key that follows the event, not its position: an earlier event arriving live shifts every
 * index, and an open traceback must stay with its own row. Events have no id of their own, so
 * the key is what tells them apart, plus a count for identical ones.
 */
function rowKeys(events: TaskEvent[]): string[] {
  const seen = new Map<string, number>()
  return events.map((event) => {
    const identity = `${event.state}|${event.retries}|${event.timestamp}`
    const count = seen.get(identity) ?? 0
    seen.set(identity, count + 1)
    return `${identity}|${count}`
  })
}

const isFailureLike = (event: TaskEvent): boolean =>
  event.state === 'FAILURE' || event.state === 'RETRY'

/**
 * Every event in time order. Only the final failure or retry is loud; all others are quiet, so
 * the one row that matters is the one that stands out. States are the raw Celery event names.
 */
export function EventHistory({ events }: { events: TaskEvent[] }): ReactElement {
  const ordered = sortEventsChronologically(events)
  const loudIndex = ordered.map(isFailureLike).lastIndexOf(true)
  const keys = rowKeys(ordered)

  return (
    <Panel title="Event history">
      <ol className="m-0 list-none px-4 pt-1 pb-3.5">
        {ordered.map((event, index) => (
          <EventRow key={keys[index]} event={event} loud={index === loudIndex} />
        ))}
      </ol>
    </Panel>
  )
}

function EventRow({ event, loud }: { event: TaskEvent; loud: boolean }): ReactElement {
  const [open, setOpen] = useState(false)
  // A REJECTED event carries Reject's reason as its exception, without a traceback.
  const reason = (isFailureLike(event) || event.state === 'REJECTED') && event.exception
  const traceback = isFailureLike(event) && event.traceback

  return (
    <li data-testid="event-row" className="grid grid-cols-[92px_minmax(0,1fr)] gap-x-2.5 py-1.5">
      <StatusChip state={event.state} label={event.state} quiet={!loud} />
      <div className="min-w-0 text-xs">
        <span className="text-muted">{formatTime(event.timestamp)}</span>
        {event.retries > 0 && <span className="ml-2.5 text-muted">retry #{event.retries}</span>}
        {reason && <div className="mt-0.5 text-muted [overflow-wrap:anywhere]">{reason}</div>}
        {traceback && (
          <>
            <button
              type="button"
              aria-expanded={open}
              onClick={() => setOpen(!open)}
              className="mt-1 block rounded-[3px] text-muted underline hover:text-ink"
            >
              {open ? 'Hide traceback' : 'Show traceback'}
            </button>
            {open && (
              <pre className="mt-2 mb-0 max-w-full overflow-x-auto rounded-[3px] bg-ground px-3 py-2.5 font-mono whitespace-pre">
                {traceback}
              </pre>
            )}
          </>
        )}
      </div>
    </li>
  )
}
