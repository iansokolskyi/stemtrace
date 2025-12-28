import { createFileRoute, Link } from '@tanstack/react-router'
import { useState } from 'react'
import type { TaskEvent } from '@/api/client'
import { useTask } from '@/api/queries'
import { TaskStateBadge } from '@/components/TaskStateBadge'
import { buildSegments, formatDuration, stateColors, TimelineBar } from '@/components/TimelineBar'

export const Route = createFileRoute('/tasks/$taskId')({
  component: TaskDetailPage,
})

function TaskDetailPage() {
  const { taskId } = Route.useParams()
  const { data, isLoading, error } = useTask(taskId)

  if (isLoading) {
    return <TaskDetailSkeleton />
  }

  if (error || !data) {
    return (
      <div className="text-center py-12">
        <p className="text-red-400">Failed to load task</p>
        <Link to="/" className="text-blue-400 hover:underline mt-2 inline-block">
          Back to tasks
        </Link>
      </div>
    )
  }

  const { task, children } = data
  const startedEvent = task.events.find((e) => e.state === 'STARTED')
  const hasParams = startedEvent?.args || startedEvent?.kwargs
  const successEvent = task.events.find((e) => e.state === 'SUCCESS')
  const hasResult = successEvent?.result !== null && successEvent?.result !== undefined

  return (
    <div className="space-y-6">
      {/* Breadcrumb */}
      <nav className="flex items-center gap-2 text-sm">
        <Link to="/" className="text-slate-400 hover:text-slate-200">
          Tasks
        </Link>
        <span className="text-slate-600">/</span>
        <span className="text-slate-200 font-mono">{taskId.slice(0, 8)}</span>
      </nav>

      {/* Task header */}
      <div className="bg-slate-900 rounded-xl border border-slate-800 p-6">
        <div className="flex items-start justify-between">
          <div>
            <h1 className="text-xl font-semibold text-slate-100">{task.name}</h1>
            <p className="text-sm text-slate-500 font-mono mt-1">{task.task_id}</p>
          </div>
          <TaskStateBadge state={task.state} />
        </div>

        <dl className="grid grid-cols-4 gap-4 mt-6">
          <InfoCard label="Duration" value={formatDuration(task.duration_ms ?? 0)} />
          <InfoCard label="First seen" value={formatTime(task.first_seen)} />
          <InfoCard label="Last update" value={formatTime(task.last_updated)} />
          <InfoCard label="Children" value={String(children.length)} />
        </dl>
      </div>

      {/* Parameters */}
      {hasParams && (
        <div className="bg-slate-900 rounded-xl border border-slate-800 p-6">
          <h2 className="text-lg font-semibold text-slate-100 mb-4">Parameters</h2>
          {startedEvent?.args && (
            <div className="mb-4">
              <span className="text-xs text-slate-500">args</span>
              <div className="bg-slate-950 rounded-lg p-3 mt-1 font-mono text-sm text-slate-300 overflow-x-auto">
                <pre>{JSON.stringify(startedEvent.args, null, 2)}</pre>
              </div>
            </div>
          )}
          {startedEvent?.kwargs && Object.keys(startedEvent.kwargs).length > 0 && (
            <div>
              <span className="text-xs text-slate-500">kwargs</span>
              <div className="bg-slate-950 rounded-lg p-3 mt-1 font-mono text-sm text-slate-300 overflow-x-auto">
                <pre>{JSON.stringify(startedEvent.kwargs, null, 2)}</pre>
              </div>
            </div>
          )}
        </div>
      )}

      {/* Result */}
      {hasResult && (
        <div className="bg-slate-900 rounded-xl border border-slate-800 p-6">
          <h2 className="text-lg font-semibold text-slate-100 mb-4">Result</h2>
          <div className="bg-slate-950 rounded-lg p-3 font-mono text-sm text-emerald-400 overflow-x-auto">
            <pre>{JSON.stringify(successEvent?.result, null, 2)}</pre>
          </div>
        </div>
      )}

      {/* Visual Timeline */}
      {task.events.length > 1 && (
        <div className="bg-slate-900 rounded-xl border border-slate-800 p-6">
          <h2 className="text-lg font-semibold text-slate-100 mb-4">Execution Timeline</h2>
          <TaskExecutionTimeline events={task.events} />
        </div>
      )}

      {/* Event timeline */}
      <div className="bg-slate-900 rounded-xl border border-slate-800 p-6">
        <h2 className="text-lg font-semibold text-slate-100 mb-4">Event History</h2>
        <div className="space-y-2">
          {sortEventsByTime(task.events).map((event, index) => (
            <EventRow key={index} event={event} />
          ))}
        </div>
      </div>

      {/* Children */}
      {children.length > 0 && (
        <div className="bg-slate-900 rounded-xl border border-slate-800 p-6">
          <h2 className="text-lg font-semibold text-slate-100 mb-4">Child Tasks</h2>
          <div className="space-y-2">
            {children.map((child) => (
              <Link
                key={child.task_id}
                to="/tasks/$taskId"
                params={{ taskId: child.task_id }}
                className="flex items-center justify-between p-3 bg-slate-800/50 rounded-lg hover:bg-slate-800 transition-colors"
              >
                <div className="flex-1 truncate">
                  <span className="text-slate-200">{child.name.split('.').pop()}</span>
                  <span className="text-slate-500 text-xs ml-2">{child.task_id.slice(0, 8)}</span>
                </div>
                <TaskStateBadge state={child.state} />
              </Link>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}

function EventRow({ event }: { event: TaskEvent }) {
  const [expanded, setExpanded] = useState(false)
  const hasTraceback = event.state === 'FAILURE' || event.state === 'RETRY'
  const showError = hasTraceback && event.exception

  const rowContent = (
    <>
      <span className="text-slate-500 font-mono text-xs">{formatTime(event.timestamp)}</span>
      <TaskStateBadge state={event.state} />
      {event.retries > 0 && <span className="text-xs text-slate-500">retry #{event.retries}</span>}
      {showError && <span className="text-xs text-red-400 truncate flex-1">{event.exception}</span>}
      {hasTraceback && event.traceback && (
        <span className="text-xs text-slate-500 ml-auto">
          {expanded ? '▲ collapse' : '▼ expand'}
        </span>
      )}
    </>
  )

  return (
    <div className="border-l-2 border-slate-700 pl-4 py-1">
      {hasTraceback && event.traceback ? (
        <button
          type="button"
          className="flex items-center gap-4 text-sm cursor-pointer w-full text-left hover:bg-slate-800/30 rounded -ml-1 pl-1 py-0.5 transition-colors"
          onClick={() => setExpanded(!expanded)}
        >
          {rowContent}
        </button>
      ) : (
        <div className="flex items-center gap-4 text-sm">{rowContent}</div>
      )}

      {expanded && event.traceback && (
        <div className="mt-2 bg-slate-950 rounded-lg p-3 font-mono text-xs text-red-300 overflow-x-auto whitespace-pre">
          {event.traceback}
        </div>
      )}
    </div>
  )
}

function InfoCard({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt className="text-xs text-slate-500">{label}</dt>
      <dd className="text-sm text-slate-200 font-mono mt-1">{value}</dd>
    </div>
  )
}

function formatTime(timestamp: string | null | undefined): string {
  if (!timestamp) return '-'
  return new Date(timestamp).toLocaleTimeString()
}

// State priority within a retry cycle: PENDING → RECEIVED → STARTED → RETRY → terminal
const STATE_PRIORITY: Record<string, number> = {
  PENDING: 0,
  RECEIVED: 1,
  STARTED: 2,
  RETRY: 3,
  SUCCESS: 10,
  FAILURE: 10,
  REVOKED: 10,
  REJECTED: 10,
}

function sortEventsByTime(events: TaskEvent[]): TaskEvent[] {
  // Filter: only keep first PENDING and first RECEIVED (retry re-queues create duplicates)
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

  // Sort by logical order: (retry_count, state_priority, timestamp)
  // This ensures correct ordering regardless of timing issues
  return filtered.sort((a, b) => {
    const retryA = a.retries ?? 0
    const retryB = b.retries ?? 0

    // 1. Sort by retry count first
    if (retryA !== retryB) return retryA - retryB

    // 2. Within same retry, sort by state lifecycle
    const priorityA = STATE_PRIORITY[a.state] ?? 5
    const priorityB = STATE_PRIORITY[b.state] ?? 5
    if (priorityA !== priorityB) return priorityA - priorityB

    // 3. Same retry and state: use timestamp as tiebreaker
    return new Date(a.timestamp).getTime() - new Date(b.timestamp).getTime()
  })
}

function TaskExecutionTimeline({ events }: { events: TaskEvent[] }) {
  const [hoveredIndex, setHoveredIndex] = useState<number | null>(null)
  const segments = buildSegments(events)

  if (segments.length === 0) return null

  const sorted = [...events].sort(
    (a, b) => new Date(a.timestamp).getTime() - new Date(b.timestamp).getTime(),
  )

  return (
    <div className="space-y-4">
      <div className="bg-slate-800/50 rounded h-10">
        <TimelineBar events={events} height="lg" showLabels />
      </div>

      {/* Time markers */}
      <div className="flex justify-between text-xs text-slate-500">
        <span>{formatTime(sorted[0].timestamp)}</span>
        <span>{formatTime(sorted[sorted.length - 1].timestamp)}</span>
      </div>

      {/* Duration breakdown */}
      <div className="flex flex-wrap gap-4 text-sm">
        {segments.map((segment, i) => {
          const isHovered = hoveredIndex === i
          return (
            <div
              key={i}
              role="presentation"
              className={`flex items-center gap-2 transition-opacity cursor-default ${hoveredIndex !== null && !isHovered ? 'opacity-50' : ''}`}
              onMouseEnter={() => setHoveredIndex(i)}
              onMouseLeave={() => setHoveredIndex(null)}
            >
              <div
                className={`rounded transition-transform ${stateColors[segment.state] ?? 'bg-gray-500'} ${segment.isTerminal ? 'w-2 h-4' : 'w-4 h-4'} ${isHovered ? 'scale-110' : ''}`}
              />
              <span className="text-slate-400">{segment.state}</span>
              <span className="text-slate-200 font-mono font-medium">
                {formatDuration(segment.duration)}
              </span>
            </div>
          )
        })}
      </div>
    </div>
  )
}

function TaskDetailSkeleton() {
  return (
    <div className="space-y-6 animate-pulse">
      <div className="h-4 w-32 bg-slate-800 rounded" />
      <div className="bg-slate-900 rounded-xl border border-slate-800 p-6">
        <div className="h-6 w-64 bg-slate-800 rounded" />
        <div className="h-4 w-48 bg-slate-800 rounded mt-2" />
        <div className="grid grid-cols-4 gap-4 mt-6">
          {[...Array(4)].map((_, i) => (
            <div key={i}>
              <div className="h-3 w-16 bg-slate-800 rounded" />
              <div className="h-4 w-24 bg-slate-800 rounded mt-2" />
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}
