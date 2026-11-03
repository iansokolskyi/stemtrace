import { Link } from '@tanstack/react-router'
import type { ReactElement } from 'react'
import type { Anomaly } from '@/api/anomalies'
import { Icon } from '@/components/Icon'
import { useCarriedRange } from '@/hooks/useCarriedRange'
import { shortHost } from '@/utils/format'
import { ACTION } from './anomalies'

interface AnomalyActionsProps {
  anomaly: Anomaly
  /**
   * Where the links are shown. The list also links the task and its runs; the task page
   * is the task, so it keeps only the link onward (the worker, or the registry).
   */
  from: 'list' | 'task'
}

/** The links an anomaly offers: task, worker, Registry, and Executions filtered to what it is about. */
export function AnomalyActions({ anomaly, from }: AnomalyActionsProps): ReactElement {
  const carried = useCarriedRange()
  const { kind, task_id: taskId, name, hostname } = anomaly
  const list = from === 'list'

  return (
    <div className="flex min-w-0 max-w-full flex-wrap items-center gap-1.5">
      {list && taskId && (
        <Link to="/tasks/$taskId" params={{ taskId }} className={ACTION}>
          <Icon name="play" size={13} />
          Open task
        </Link>
      )}
      {hostname && (kind === 'stuck' || kind === 'interrupted') && (
        <Link to="/workers/$hostname" params={{ hostname }} className={ACTION}>
          <Icon name="server" size={13} className="flex-none" />
          <span className="min-w-0 truncate" title={hostname}>
            Worker {shortHost(hostname)}
          </span>
        </Link>
      )}
      {kind === 'unregistered' && (
        <Link to="/registry" className={ACTION}>
          <Icon name="book" size={13} />
          Registry
        </Link>
      )}
      {kind === 'no_workers_online' && (
        <>
          <Link to="/workers" className={ACTION}>
            <Icon name="server" size={13} />
            Workers
          </Link>
          <Link to="/executions" search={{ state: 'PENDING', ...carried }} className={ACTION}>
            <Icon name="list" size={13} />
            Waiting tasks
          </Link>
        </>
      )}
      {list && name && (
        <Link to="/executions" search={{ name_exact: name, ...carried }} className={ACTION}>
          <Icon name="list" size={13} />
          All runs
        </Link>
      )}
    </div>
  )
}
