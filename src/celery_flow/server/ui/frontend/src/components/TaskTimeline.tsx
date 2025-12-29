import { Link } from '@tanstack/react-router'
import { clsx } from 'clsx'
import type { TaskNode } from '@/api/client'
import { useTasks } from '@/api/queries'
import { formatDuration, stateColors, TERMINAL_STATES, TimelineBar } from './TimelineBar'

interface TaskTimelineProps {
  filters: {
    state: string | undefined
    name: string
    from_date?: string
    to_date?: string
  }
}

export function TaskTimeline({ filters }: TaskTimelineProps) {
  const { data, isLoading, error } = useTasks({
    state: filters.state,
    name: filters.name || undefined,
    from_date: filters.from_date,
    to_date: filters.to_date,
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
      <div className="flex justify-between text-xs text-slate-500 mb-4 pl-48">
        <span>{formatTime(new Date(minTime))}</span>
        <span>{formatTime(new Date(minTime + range / 2))}</span>
        <span>{formatTime(new Date(maxTime))}</span>
      </div>

      <div className="space-y-2">
        {tasks.map((task) => (
          <TaskTimelineRow key={task.task_id} task={task} minTime={minTime} range={range} />
        ))}
      </div>

      <div className="flex flex-wrap gap-4 mt-6 pt-4 border-t border-slate-800 text-xs text-slate-400">
        {Object.entries(stateColors).map(([state, color]) => (
          <div key={state} className="flex items-center gap-1.5">
            <div
              className={clsx('h-3 rounded', color, TERMINAL_STATES.has(state) ? 'w-2' : 'w-3')}
            />
            <span>{state}</span>
          </div>
        ))}
      </div>
    </div>
  )
}

interface TaskTimelineRowProps {
  task: TaskNode
  minTime: number
  range: number
}

function TaskTimelineRow({ task, minTime, range }: TaskTimelineRowProps) {
  const taskStart = task.first_seen ? new Date(task.first_seen).getTime() : minTime
  const taskStartPct = ((taskStart - minTime) / range) * 100
  const wouldOverflow = taskStartPct > 85

  return (
    <div className="flex items-center gap-4 h-8 group">
      <Link
        to="/tasks/$taskId"
        params={{ taskId: task.task_id }}
        className="w-44 truncate text-sm text-slate-300 hover:text-emerald-400 transition-colors"
        title={task.name}
      >
        {task.name.split('.').pop()}
      </Link>

      <div className="flex-1 relative h-6">
        <div className="absolute inset-0 bg-slate-800/50 rounded" />
        <div
          className="absolute h-full"
          style={
            wouldOverflow
              ? { right: 0, minWidth: '80px', maxWidth: '200px' }
              : { left: `${taskStartPct}%`, minWidth: '80px', maxWidth: '200px' }
          }
        >
          <TimelineBar events={task.events} height="sm" />
        </div>
      </div>

      <div className="w-20 text-right text-xs text-slate-500 font-mono">
        {task.duration_ms !== null ? formatDuration(task.duration_ms) : '-'}
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
            <div className="w-20 h-4 bg-slate-800 rounded" />
          </div>
        ))}
      </div>
    </div>
  )
}
