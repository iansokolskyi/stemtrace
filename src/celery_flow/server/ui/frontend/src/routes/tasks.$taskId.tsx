import { createFileRoute, Link } from '@tanstack/react-router'
import { useTask } from '@/api/queries'
import { TaskStateBadge } from '@/components/TaskStateBadge'

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

        <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mt-6">
          <InfoCard label="Duration" value={task.duration_ms ? `${task.duration_ms}ms` : '-'} />
          <InfoCard label="First seen" value={formatTime(task.first_seen)} />
          <InfoCard label="Last update" value={formatTime(task.last_updated)} />
          <InfoCard label="Children" value={children.length.toString()} />
        </div>
      </div>

      {/* Event timeline */}
      <div className="bg-slate-900 rounded-xl border border-slate-800 p-6">
        <h2 className="text-lg font-semibold text-slate-100 mb-4">Event History</h2>
        <div className="space-y-3">
          {task.events.map((event, index) => (
            <div key={index} className="flex items-center gap-4 text-sm">
              <span className="text-slate-500 font-mono w-24">{formatTime(event.timestamp)}</span>
              <TaskStateBadge state={event.state} />
              {event.retries > 0 && (
                <span className="text-amber-400 text-xs">retry #{event.retries}</span>
              )}
            </div>
          ))}
        </div>
      </div>

      {/* Children */}
      {children.length > 0 && (
        <div className="bg-slate-900 rounded-xl border border-slate-800 p-6">
          <h2 className="text-lg font-semibold text-slate-100 mb-4">
            Child Tasks ({children.length})
          </h2>
          <div className="space-y-2">
            {children.map((child) => (
              <Link
                key={child.task_id}
                to="/tasks/$taskId"
                params={{ taskId: child.task_id }}
                className="flex items-center justify-between p-3 rounded-lg bg-slate-800/50 hover:bg-slate-800 transition-colors"
              >
                <div>
                  <span className="text-slate-200">{child.name}</span>
                  <span className="text-slate-500 font-mono text-xs ml-2">
                    {child.task_id.slice(0, 8)}
                  </span>
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
