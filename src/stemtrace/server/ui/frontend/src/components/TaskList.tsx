/**
 * Task list component with infinite scroll.
 */

import { Link } from '@tanstack/react-router'
import { useTasksInfinite } from '@/api/queries'
import { useInfiniteScroll } from '@/hooks/useInfiniteScroll'
import { TaskStateBadge } from './TaskStateBadge'

interface TaskListProps {
  filters: {
    state: string | undefined
    name: string
    from_date?: string
    to_date?: string
  }
}

export function TaskList({ filters }: TaskListProps) {
  const { data, isLoading, error, hasNextPage, isFetchingNextPage, fetchNextPage } =
    useTasksInfinite({
      state: filters.state,
      name: filters.name || undefined,
      from_date: filters.from_date,
      to_date: filters.to_date,
    })

  const { lastElementRef } = useInfiniteScroll({
    hasNextPage,
    isFetchingNextPage,
    fetchNextPage,
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

  const allTasks = data?.pages.flatMap((page) => page.tasks) ?? []
  const total = data?.pages[0]?.total ?? 0

  if (!allTasks.length) {
    return <EmptyState />
  }

  return (
    <div className="space-y-2">
      {allTasks.map((task, index) => {
        const isLast = index === allTasks.length - 1
        return (
          <Link
            key={task.task_id}
            ref={isLast ? lastElementRef : undefined}
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
        )
      })}

      {/* Loading indicator */}
      {isFetchingNextPage && <LoadingMore />}

      {/* Pagination info */}
      <div className="text-center text-sm text-slate-500 pt-4">
        Showing {allTasks.length} of {total} tasks
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

function LoadingMore() {
  return (
    <div className="flex justify-center py-4">
      <div className="flex items-center gap-2 text-sm text-slate-400">
        <svg
          className="animate-spin h-4 w-4"
          xmlns="http://www.w3.org/2000/svg"
          fill="none"
          viewBox="0 0 24 24"
          aria-hidden="true"
        >
          <circle
            className="opacity-25"
            cx="12"
            cy="12"
            r="10"
            stroke="currentColor"
            strokeWidth="4"
          />
          <path
            className="opacity-75"
            fill="currentColor"
            d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"
          />
        </svg>
        Loading more...
      </div>
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
