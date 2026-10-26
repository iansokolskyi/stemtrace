import { type ReactElement, useEffect, useState } from 'react'
import type { TaskEvent } from '@/api/client'
import { StatusChip } from '@/components/StatusChip'
import { sortEventsChronologically } from '@/components/TimelineBar'
import { Panel } from '@/components/task/Panel'
import { formatTime } from '@/utils/format'

interface TaskOutputProps {
  events: TaskEvent[]
}

/**
 * What each attempt printed and logged (captured when the worker runs with
 * `capture_output=True`). One block per attempt, latest first; renders nothing
 * when no attempt has output.
 */
export function TaskOutput({ events }: TaskOutputProps): ReactElement | null {
  // Newest attempt first, by retry number, then time: attempts can run on
  // workers whose clocks disagree. Events carrying output always come from the
  // process that ran the attempt, so their retry number is reliable (unlike
  // main-process failures, which report retries=0 but never carry output).
  const attempts = sortEventsChronologically(events)
    .filter((event) => event.output)
    .map((event, order) => ({ event, order }))
    .sort((a, b) => (b.event.retries ?? 0) - (a.event.retries ?? 0) || b.order - a.order)
    .map(({ event }) => event)

  if (attempts.length === 0) return null

  return (
    <div data-testid="task-output">
      <Panel title="Output">
        <div className="space-y-3 px-4 pt-1 pb-3.5">
          {attempts.map((event, index) => (
            <OutputAttempt
              key={`${event.state}-${event.retries}-${event.timestamp}`}
              event={event}
              defaultExpanded={index === 0}
            />
          ))}
        </div>
      </Panel>
    </div>
  )
}

interface OutputAttemptProps {
  event: TaskEvent
  defaultExpanded: boolean
}

function OutputAttempt({ event, defaultExpanded }: OutputAttemptProps): ReactElement {
  const [expanded, setExpanded] = useState(defaultExpanded)
  const output = event.output ?? ''

  return (
    <div data-testid="output-attempt" className="min-w-0">
      <div className="flex items-center gap-3 text-xs">
        <button
          type="button"
          className="flex min-w-0 flex-1 items-center gap-3 rounded-[3px] py-0.5 text-left hover:bg-hover"
          onClick={() => setExpanded(!expanded)}
          aria-expanded={expanded}
        >
          <StatusChip state={event.state} label={event.state} quiet />
          <span className="text-muted">{formatTime(event.timestamp)}</span>
          {event.retries > 0 && <span className="text-muted">retry #{event.retries}</span>}
          <span className="ml-auto text-muted">{expanded ? 'collapse' : 'expand'}</span>
        </button>
        <CopyButton text={output} />
      </div>

      {expanded && (
        <pre
          className="mt-2 mb-0 max-h-96 max-w-full overflow-auto rounded-[3px] bg-ground px-3 py-2.5 font-mono text-xs leading-relaxed whitespace-pre"
          data-testid="output-text"
        >
          {output}
        </pre>
      )}
    </div>
  )
}

function CopyButton({ text }: { text: string }): ReactElement {
  const [copied, setCopied] = useState(false)

  useEffect(() => {
    if (!copied) return
    const timer = setTimeout(() => setCopied(false), 1500)
    return () => clearTimeout(timer)
  }, [copied])

  const copy = async (): Promise<void> => {
    try {
      await navigator.clipboard.writeText(text)
      setCopied(true)
    } catch {
      // Clipboard unavailable (insecure context or denied); nothing to do.
    }
  }

  return (
    <button
      type="button"
      onClick={copy}
      className="rounded-[3px] px-1.5 py-0.5 text-muted hover:bg-hover hover:text-ink"
    >
      {copied ? 'Copied' : 'Copy'}
    </button>
  )
}
