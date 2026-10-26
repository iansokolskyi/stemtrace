import { type ReactElement, useState } from 'react'
import type { TaskEvent } from '@/api/client'
import { sortEventsChronologically } from '@/components/TimelineBar'
import { Panel } from './Panel'

interface ExceptionBlockProps {
  events: TaskEvent[]
  /** The class of the latest exception, when the API knows it. */
  exceptionType?: string | null
}

/**
 * The exception of the final failure or retry: its type and message, and the traceback behind a
 * toggle. Nothing when the task never failed or retried.
 */
export function ExceptionBlock({
  events,
  exceptionType,
}: ExceptionBlockProps): ReactElement | null {
  const [open, setOpen] = useState(false)
  const last = sortEventsChronologically(events)
    .filter((event) => event.state === 'FAILURE' || event.state === 'RETRY')
    .at(-1)
  if (!last || (!last.exception && !last.traceback)) return null

  return (
    <Panel
      title="Exception"
      aside={exceptionType && <span className="text-xs text-failure-t">{exceptionType}</span>}
    >
      {last.exception && (
        <p className="m-0 px-4 pt-0 pb-2.5 text-xs [overflow-wrap:anywhere]">{last.exception}</p>
      )}
      {last.traceback && (
        <div className={open ? undefined : 'pb-3.5'}>
          <button
            type="button"
            aria-expanded={open}
            onClick={() => setOpen(!open)}
            className="mx-4 inline-flex h-[26px] items-center rounded-[3px] bg-raised px-2.5 text-xs font-semibold hover:bg-press"
          >
            {open ? 'Hide traceback' : 'Show traceback'}
          </button>
          {open && (
            <pre className="m-0 mt-3 max-w-full overflow-x-auto rounded-b-lg bg-ground px-4 py-3.5 font-mono text-xs leading-relaxed whitespace-pre">
              {last.traceback}
            </pre>
          )}
        </div>
      )}
    </Panel>
  )
}
