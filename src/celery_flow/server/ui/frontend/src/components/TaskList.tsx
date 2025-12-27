/**
 * Task list component with pagination.
 */

import { Link } from '@tanstack/react-router'
import { useTasks } from '@/api/queries'
import { TaskStateBadge } from './TaskStateBadge'

interface TaskListProps {
  filters: {
    state: string | undefined
    name: string
  }
}

export function TaskList({ filters }: TaskListProps) {
  const { data, isLoading, error } = useTasks({
    state: filters.state,
    name: filters.name || undefined,
    limit: 50,
  })

  if (isLoading) {
    return <TaskListSkeleton />
  }

  if (error) {
    return (
      <div className="text-center py-12 text-red-400">
        Failed to load tasks. Is the server running?
      </div>
    )
  }

  if (!data?.tasks.length) {
    return <EmptyState />
  }

  return (
    <div className="space-y-2">
      {data.tasks.map((task) => (
        <Link
          key={task.task_id}
          to="/tasks/$taskId"
          params={{ taskId: task.task_id }}
          className="flex items-center justify-between p-4 bg-slate-900 rounded-lg border border-slate-800 hover:border-slate-700 transition-colors"
        >
          <div className="flex items-center gap-4 min-w-0">
            <TaskStateBadge state={task.state} />
            <div className="min-w-0">
              <p className="text-slate-200 truncate">{task.name}</p>
              <p className="text-xs text-slate-500 font-mono">{task.task_id.slice(0, 8)}...</p>
            </div>
          </div>

          <div className="flex items-center gap-6 text-sm text-slate-400">
            {task.duration_ms !== null && <span className="font-mono">{task.duration_ms}ms</span>}
            {task.children.length > 0 && (
              <span className="text-xs bg-slate-800 px-2 py-0.5 rounded">
                {task.children.length} children
              </span>
            )}
            <span className="text-xs">{formatRelativeTime(task.last_updated)}</span>
          </div>
        </Link>
      ))}

      {/* Pagination info */}
      <div className="text-center text-sm text-slate-500 pt-4">
        Showing {data.tasks.length} of {data.total} tasks
      </div>
    </div>
  )
}

function formatRelativeTime(timestamp: string | null): string {
  if (!timestamp) return ''
  const date = new Date(timestamp)
  const now = new Date()
  const diff = now.getTime() - date.getTime()

  if (diff < 60000) return 'just now'
  if (diff < 3600000) return `${Math.floor(diff / 60000)}m ago`
  if (diff < 86400000) return `${Math.floor(diff / 3600000)}h ago`
  return date.toLocaleDateString()
}

function EmptyState() {
  return (
    <div className="text-center py-16 bg-slate-900/50 rounded-xl border border-slate-800">
      <div className="text-4xl mb-4">📋</div>
      <h3 className="text-lg font-medium text-slate-200">No tasks found</h3>
      <p className="text-sm text-slate-500 mt-1">
        Try adjusting your filters or run some Celery tasks
      </p>
    </div>
  )
}

function TaskListSkeleton() {
  return (
    <div className="space-y-2 animate-pulse">
      {[...Array(5)].map((_, i) => (
        <div
          key={i}
          className="flex items-center justify-between p-4 bg-slate-900 rounded-lg border border-slate-800"
        >
          <div className="flex items-center gap-4">
            <div className="w-16 h-5 bg-slate-800 rounded" />
            <div>
              <div className="w-48 h-4 bg-slate-800 rounded" />
              <div className="w-24 h-3 bg-slate-800 rounded mt-2" />
            </div>
          </div>
          <div className="w-16 h-4 bg-slate-800 rounded" />
        </div>
      ))}
    </div>
  )
}
