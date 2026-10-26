import { createFileRoute, Link } from '@tanstack/react-router'
import { useTask } from '@/api/queries'
import { TaskOutput } from '@/components/TaskOutput'
import { TaskStateBadge } from '@/components/TaskStateBadge'
import { EventHistory } from '@/components/task/EventHistory'
import { ExceptionBlock } from '@/components/task/ExceptionBlock'
import { ExecutionTimeline } from '@/components/task/ExecutionTimeline'
import { TaskArguments, TaskResult } from '@/components/task/KwargsList'
import { TaskFacts } from '@/components/task/TaskFacts'
import { TaskHeader } from '@/components/task/TaskHeader'
import { TimingBar } from '@/components/task/TimingBar'
import { shortestUniqueSuffixes } from '@/utils/taskNames'

export const Route = createFileRoute('/tasks/$taskId')({
  component: TaskDetailPage,
  staticData: { section: { label: 'Executions', to: '/executions' } },
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
        <Link to="/executions" className="text-blue-400 hover:underline mt-2 inline-block">
          Back to executions
        </Link>
      </div>
    )
  }

  const { task, children } = data
  const childNames = shortestUniqueSuffixes(children.map((child) => child.name))
  return (
    <div className="space-y-6">
      <TaskHeader task={task} />
      <TimingBar state={task.state} queueWaitMs={task.queue_wait_ms} runtimeMs={task.runtime_ms} />
      <TaskFacts task={task} />

      <ExceptionBlock events={task.events} exceptionType={task.exception_type} />
      <TaskArguments events={task.events} />
      <TaskResult events={task.events} />

      {/* Output per attempt (worker capture_output=True) */}
      <TaskOutput events={task.events} />

      <ExecutionTimeline events={task.events} />
      <EventHistory events={task.events} />

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
                  <span className="text-slate-200" title={child.name}>
                    {childNames.get(child.name) ?? child.name}
                  </span>
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
