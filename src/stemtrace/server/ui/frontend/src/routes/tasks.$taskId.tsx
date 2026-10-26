import { createFileRoute, Link } from '@tanstack/react-router'
import { useTask } from '@/api/queries'
import { TaskOutput } from '@/components/TaskOutput'
import { ChildTasks } from '@/components/task/ChildTasks'
import { EventHistory } from '@/components/task/EventHistory'
import { ExceptionBlock } from '@/components/task/ExceptionBlock'
import { ExecutionTimeline } from '@/components/task/ExecutionTimeline'
import { TaskArguments, TaskResult } from '@/components/task/KwargsList'
import { retryCount } from '@/components/task/retryCount'
import { TaskFacts } from '@/components/task/TaskFacts'
import { TaskHeader } from '@/components/task/TaskHeader'
import { TimingBar } from '@/components/task/TimingBar'

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
      <div className="py-12 text-center">
        <p className="text-failure-t">Failed to load task</p>
        <Link to="/executions" className="mt-2 inline-block underline hover:text-muted">
          Back to executions
        </Link>
      </div>
    )
  }

  const { task, children } = data
  return (
    <div>
      <TaskHeader task={task} />
      <div className="grid items-start gap-x-4 xl:grid-cols-[minmax(0,1.6fr)_minmax(0,1fr)]">
        <div className="min-w-0">
          <TimingBar
            state={task.state}
            queueWaitMs={task.queue_wait_ms}
            runtimeMs={task.runtime_ms}
            totalRuntimeMs={task.total_runtime_ms}
            retries={retryCount(task)}
          />
          <ExecutionTimeline events={task.events} />
          <ExceptionBlock events={task.events} exceptionType={task.exception_type} />
          <TaskResult events={task.events} />
          <TaskOutput events={task.events} />
        </div>
        <div className="min-w-0">
          <TaskFacts task={task} />
          <TaskArguments events={task.events} />
          <EventHistory events={task.events} />
          <ChildTasks tasks={children} />
        </div>
      </div>
    </div>
  )
}

/** The page's own shape while the task loads: a title bar and two panels, no motion. */
function TaskDetailSkeleton() {
  return (
    <div aria-hidden="true">
      <div className="mb-2 h-3 w-40 rounded-[3px] bg-raised" />
      <div className="mb-3 h-6 w-64 rounded-[3px] bg-raised" />
      <div className="mb-4 h-5 w-80 max-w-full rounded-[3px] bg-raised" />
      <div className="mb-4 h-24 rounded-lg bg-surface" />
      <div className="h-40 rounded-lg bg-surface" />
    </div>
  )
}
