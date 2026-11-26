import { Link } from '@tanstack/react-router'
import type { ReactElement } from 'react'
import { isNotFound } from '@/api/client'
import { useTask } from '@/api/queries'
import { Icon } from '@/components/Icon'
import { LoadFailure } from '@/components/Notice'
import { PageStale } from '@/components/PageStale'
import { Skeleton, SkeletonRegion } from '@/components/Skeleton'
import { StatusChip } from '@/components/StatusChip'
import { TaskName } from '@/components/TaskName'
import { retryCount } from '@/components/task/retryCount'
import { useViewState } from '@/hooks/useViewState'
import { sortEventsChronologically } from '@/utils/events'
import { formatDateTime, formatSpan, shortHost } from '@/utils/format'
import { usePanel } from './PanelContext'
import { BUTTON_PRIMARY, BUTTON_QUIET, Facts, Section } from './PanelParts'

/** A task in brief, with the two ways out of the panel: its page and its flow. */
export function TaskBody({ id }: { id: string }): ReactElement {
  const panel = usePanel()
  const query = useTask(id)
  const { data } = query
  const state = useViewState(query, `tasks/${id}`)
  if (state.phase === 'error') {
    if (isNotFound(state.error)) {
      return (
        <p className="m-0 pt-4 text-xs text-muted">
          This task is no longer stored. It may have left the retention window.
        </p>
      )
    }
    return <LoadFailure what="the task" view={state} />
  }
  if (!data)
    return (
      <SkeletonRegion label="task" className="pt-4">
        <Skeleton className="mb-2 h-4 w-48" />
        <Skeleton className="mb-3 h-3 w-64 max-w-full" />
        <Skeleton className="h-7 w-32" />
      </SkeletonRegion>
    )

  const { task, children, flow_root_id: flowRootId } = data
  // A flow of one task (its own root, no parent, no children) has nothing more to show
  const hasFlow =
    flowRootId && (flowRootId !== task.task_id || children.length > 0 || task.parent_id)
  const failure = sortEventsChronologically(task.events)
    .filter((event) => event.state === 'FAILURE' || event.state === 'RETRY')
    .at(-1)
  const retries = retryCount(task)

  return (
    <>
      <PageStale views={[state]} className="mt-3" />
      <Section>
        <TaskName name={task.name} />
        <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1.5">
          <StatusChip state={task.state} />
          <span className="font-mono text-xs text-muted [overflow-wrap:anywhere]">
            {task.task_id}
          </span>
        </div>
        <div className="mt-2.5 flex flex-wrap gap-1.5">
          <Link to="/tasks/$taskId" params={{ taskId: task.task_id }} className={BUTTON_PRIMARY}>
            Open task page
          </Link>
          {hasFlow && (
            <Link to="/flows/$rootId" params={{ rootId: flowRootId }} className={BUTTON_QUIET}>
              <Icon name="flow" size={13} />
              Open flow
            </Link>
          )}
        </div>
      </Section>
      <Section>
        <Facts
          rows={[
            [
              'Worker',
              task.hostname && (
                <button
                  type="button"
                  onClick={() => panel.push({ kind: 'worker', id: task.hostname ?? '' })}
                  className="link font-mono"
                >
                  {shortHost(task.hostname)}
                </button>
              ),
            ],
            ['Queue', task.queue && <span className="font-mono">{task.queue}</span>],
            ['Group', task.task_group],
            ['Queue wait', task.queue_wait_ms != null && formatSpan(task.queue_wait_ms)],
            ['Runtime', task.runtime_ms != null && formatSpan(task.runtime_ms)],
            ['Retries', retries > 0 && String(retries)],
            ['First seen', formatDateTime(task.first_seen)],
          ]}
        />
      </Section>
      {failure?.exception && (
        <Section title="Exception">
          {task.exception_type && (
            <div className="font-mono text-xs font-bold text-failure-t">{task.exception_type}</div>
          )}
          <div className="font-mono text-xs [overflow-wrap:anywhere]">{failure.exception}</div>
        </Section>
      )}
    </>
  )
}
