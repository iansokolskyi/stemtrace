import { useEffect, useState } from 'react'
import type { TaskEvent } from '@/api/client'
import { TaskStateBadge } from '@/components/TaskStateBadge'
import { sortEventsChronologically } from '@/components/TimelineBar'

interface TaskOutputProps {
  events: TaskEvent[]
}

/**
 * What each attempt printed and logged (captured when the worker runs with
 * `capture_output=True`). One block per attempt, latest first; renders nothing
 * when no attempt has output.
 */
export function TaskOutput({ events }: TaskOutputProps) {
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
    <div className="bg-slate-900 rounded-xl border border-slate-800 p-6" data-testid="task-output">
      <h2 className="text-lg font-semibold text-slate-100 mb-4">Output</h2>
      <div className="space-y-3">
        {attempts.map((event, index) => (
          <OutputAttempt
            key={`${event.state}-${event.retries}-${event.timestamp}`}
            event={event}
            defaultExpanded={index === 0}
          />
        ))}
      </div>
    </div>
  )
}

interface OutputAttemptProps {
  event: TaskEvent
  defaultExpanded: boolean
}

function OutputAttempt({ event, defaultExpanded }: OutputAttemptProps) {
  const [expanded, setExpanded] = useState(defaultExpanded)
  const output = event.output ?? ''

  return (
    <div className="border-l-2 border-slate-700 pl-4" data-testid="output-attempt">
      <div className="flex items-center gap-4 text-sm">
        <button
          type="button"
          className="flex items-center gap-4 flex-1 text-left cursor-pointer hover:bg-slate-800/30 rounded -ml-1 pl-1 py-0.5 transition-colors"
          onClick={() => setExpanded(!expanded)}
          aria-expanded={expanded}
        >
          <span className="text-slate-500 font-mono text-xs">{formatTime(event.timestamp)}</span>
          <TaskStateBadge state={event.state} />
          {event.retries > 0 && (
            <span className="text-xs text-slate-500">retry #{event.retries}</span>
          )}
          <span className="text-xs text-slate-500 ml-auto">
            {expanded ? '▲ collapse' : '▼ expand'}
          </span>
        </button>
        <CopyButton text={output} />
      </div>

      {expanded && (
        <pre
          className="mt-2 bg-slate-950 rounded-lg p-3 font-mono text-xs text-slate-300 overflow-auto max-h-96 whitespace-pre"
          data-testid="output-text"
        >
          {output}
        </pre>
      )}
    </div>
  )
}

function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false)

  useEffect(() => {
    if (!copied) return
    const timer = setTimeout(() => setCopied(false), 1500)
    return () => clearTimeout(timer)
  }, [copied])

  const copy = async () => {
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
      className="text-xs text-slate-500 hover:text-slate-300 transition-colors"
    >
      {copied ? 'Copied' : 'Copy'}
    </button>
  )
}

function formatTime(timestamp: string | null | undefined): string {
  if (!timestamp) return '-'
  return new Date(timestamp).toLocaleTimeString()
}
