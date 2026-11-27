import { createFileRoute, Link } from '@tanstack/react-router'
import { isNotFound } from '@/api/client'
import { useTask } from '@/api/queries'
import { AnomalyCallout } from '@/components/anomalies/AnomalyCallout'
import { LoadFailure, Notice } from '@/components/Notice'
import { PageStale } from '@/components/PageStale'
import { Skeleton, SkeletonRegion } from '@/components/Skeleton'
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
import { useCarriedRange } from '@/hooks/useCarriedRange'
import { useViewState } from '@/hooks/useViewState'

export const Route = createFileRoute('/tasks/$taskId')({
  component: TaskDetailPage,
  staticData: { section: { label: 'Executions', to: '/executions' } },
})

function TaskDetailPage() {
  const carried = useCarriedRange()
  const { taskId } = Route.useParams()
  const query = useTask(taskId)
  const view = useViewState(query, `tasks/${taskId}`, 'task')
  const { data } = query

  if (view.phase === 'loading') return <TaskDetailSkeleton />

  if (view.phase === 'error' || !data) {
    const back = (
      <Link to="/executions" search={carried} className="link">
        Back to executions
      </Link>
    )
    if (isNotFound(view.error)) {
      return (
        <Notice action={back}>
          This task is no longer stored. It may have left the retention window.
        </Notice>
      )
    }
    return <LoadFailure what="the task" view={view} action={back} />
  }

  const { task, children } = data
  return (
    <div>
      <PageStale views={[view]} />
      <TaskHeader task={task} flowRootId={data.flow_root_id} childCount={children.length} />
      {data.anomaly_detail && <AnomalyCallout anomaly={data.anomaly_detail} placement="task" />}
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

/** The page's own shape while the task loads: a title bar and two panels. */
function TaskDetailSkeleton() {
  return (
    <SkeletonRegion label="task">
      <Skeleton className="mb-2 h-3 w-40" />
      <Skeleton className="mb-3 h-6 w-64" />
      <Skeleton className="mb-4 h-5 w-80 max-w-full" />
      <Skeleton card className="mb-4 h-24" />
      <Skeleton card className="h-40" />
    </SkeletonRegion>
  )
}
