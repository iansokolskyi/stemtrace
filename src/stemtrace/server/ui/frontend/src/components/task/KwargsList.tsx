import type { ReactElement } from 'react'
import type { TaskEvent } from '@/api/client'
import { sortEventsChronologically } from '@/components/TimelineBar'
import { Panel } from './Panel'

interface Line {
  key: string
  value: unknown
}

/** Mono key/value lines. The values are shown exactly as the API sent them (already scrubbed). */
function Lines({ lines }: { lines: Line[] }): ReactElement {
  return (
    <dl className="m-0 grid grid-cols-[minmax(0,max-content)_minmax(0,1fr)] gap-x-3 gap-y-1 px-4 pt-2 pb-3.5 font-mono text-xs">
      {lines.map(({ key, value }) => (
        <div key={key} className="contents">
          <dt className="text-muted [overflow-wrap:anywhere]">{key}</dt>
          <dd className="m-0 [overflow-wrap:anywhere]">{JSON.stringify(value)}</dd>
        </div>
      ))}
    </dl>
  )
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

/** What the task was called with: its args, then each kwarg, from the first events that carry them. */
export function TaskArguments({ events }: { events: TaskEvent[] }): ReactElement | null {
  const ordered = sortEventsChronologically(events)
  const args = ordered.find((event) => event.args && event.args.length > 0)?.args
  const kwargs = ordered.find(
    (event) => event.kwargs && Object.keys(event.kwargs).length > 0,
  )?.kwargs
  const lines: Line[] = [
    ...(args ? [{ key: 'args', value: args }] : []),
    ...Object.entries(kwargs ?? {}).map(([key, value]) => ({ key, value })),
  ]
  if (lines.length === 0) return null

  return (
    <Panel title="Arguments">
      <Lines lines={lines} />
    </Panel>
  )
}

/** What a successful task returned: an object by key, anything else as one `result` line. */
export function TaskResult({ events }: { events: TaskEvent[] }): ReactElement | null {
  const success = sortEventsChronologically(events)
    .filter(
      (event) => event.state === 'SUCCESS' && event.result !== null && event.result !== undefined,
    )
    .at(-1)
  if (!success) return null
  const { result } = success
  const lines: Line[] = isPlainObject(result)
    ? Object.entries(result).map(([key, value]) => ({ key, value }))
    : [{ key: 'result', value: result }]
  if (lines.length === 0) return null

  return (
    <Panel title="Result">
      <Lines lines={lines} />
    </Panel>
  )
}
