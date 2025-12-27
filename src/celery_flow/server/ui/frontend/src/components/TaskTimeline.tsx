/**
 * Timeline/Gantt view of task execution.
 */

import { clsx } from 'clsx'
import { useTasks } from '@/api/queries'

interface TaskTimelineProps {
  filters: {
    state: string | undefined
    name: string
  }
}

const stateColors: Record<string, string> = {
  PENDING: 'bg-gray-500',
  RECEIVED: 'bg-gray-500',
  STARTED: 'bg-blue-500',
  SUCCESS: 'bg-green-500',
  FAILURE: 'bg-red-500',
  RETRY: 'bg-amber-500',
  REVOKED: 'bg-purple-500',
  REJECTED: 'bg-red-600',
}

export function TaskTimeline({ filters }: TaskTimelineProps) {
  const { data, isLoading, error } = useTasks({
    state: filters.state,
    name: filters.name || undefined,
    limit: 30,
  })

  if (isLoading) {
    return <TimelineSkeleton />
  }

  if (error) {
    return <div className="text-center py-12 text-red-400">Failed to load timeline</div>
  }

  if (!data?.tasks.length) {
    return (
      <div className="text-center py-16 bg-slate-900/50 rounded-xl border border-slate-800">
        <div className="text-4xl mb-4">📊</div>
        <h3 className="text-lg font-medium text-slate-200">No tasks to display</h3>
      </div>
    )
  }

  // Calculate time range
  const tasks = data.tasks.filter((t) => t.first_seen && t.last_updated)
  if (tasks.length === 0) {
    return <div className="text-center py-12 text-slate-400">No timing data available</div>
  }

  const times = tasks.flatMap((t) => [
    new Date(t.first_seen!).getTime(),
    new Date(t.last_updated!).getTime(),
  ])
  const minTime = Math.min(...times)
  const maxTime = Math.max(...times)
  const range = maxTime - minTime || 1

  return (
    <div className="bg-slate-900 rounded-xl border border-slate-800 p-6 overflow-x-auto">
      {/* Time axis */}
      <div className="flex justify-between text-xs text-slate-500 mb-4 pl-48">
        <span>{formatTime(new Date(minTime))}</span>
        <span>{formatTime(new Date(minTime + range / 2))}</span>
        <span>{formatTime(new Date(maxTime))}</span>
      </div>

      {/* Task bars */}
      <div className="space-y-2">
        {tasks.map((task) => {
          const start = new Date(task.first_seen!).getTime()
          const end = new Date(task.last_updated!).getTime()
          const left = ((start - minTime) / range) * 100
          const width = Math.max(((end - start) / range) * 100, 1)

          return (
            <div key={task.task_id} className="flex items-center gap-4 h-8">
              {/* Task name */}
              <div className="w-44 truncate text-sm text-slate-300" title={task.name}>
                {task.name.split('.').pop()}
              </div>

              {/* Timeline bar */}
              <div className="flex-1 relative h-6 bg-slate-800/50 rounded">
                <div
                  className={clsx(
                    'absolute h-full rounded transition-all',
                    stateColors[task.state] ?? 'bg-gray-500',
                  )}
                  style={{
                    left: `${left}%`,
                    width: `${width}%`,
                    minWidth: '4px',
                  }}
                  title={`${task.duration_ms ?? 0}ms`}
                />
              </div>

              {/* Duration */}
              <div className="w-16 text-right text-xs text-slate-500 font-mono">
                {task.duration_ms !== null ? `${task.duration_ms}ms` : '-'}
              </div>
            </div>
          )
        })}
      </div>

      {/* Legend */}
      <div className="flex gap-4 mt-6 pt-4 border-t border-slate-800 text-xs text-slate-400">
        {Object.entries(stateColors)
          .slice(0, 5)
          .map(([state, color]) => (
            <div key={state} className="flex items-center gap-1.5">
              <div className={clsx('w-3 h-3 rounded', color)} />
              <span>{state}</span>
            </div>
          ))}
      </div>
    </div>
  )
}

function formatTime(date: Date): string {
  return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })
}

function TimelineSkeleton() {
  return (
    <div className="bg-slate-900 rounded-xl border border-slate-800 p-6 animate-pulse">
      <div className="h-4 w-full bg-slate-800 rounded mb-4" />
      <div className="space-y-2">
        {[...Array(5)].map((_, i) => (
          <div key={i} className="flex items-center gap-4 h-8">
            <div className="w-44 h-4 bg-slate-800 rounded" />
            <div className="flex-1 h-6 bg-slate-800 rounded" />
            <div className="w-16 h-4 bg-slate-800 rounded" />
          </div>
        ))}
      </div>
    </div>
  )
}
