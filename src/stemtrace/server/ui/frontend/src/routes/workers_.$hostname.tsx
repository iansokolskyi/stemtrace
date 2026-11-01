import { createFileRoute, Link } from '@tanstack/react-router'
import { useMemo } from 'react'
import type { Worker } from '@/api/client'
import { useGroups, useWorkers } from '@/api/queries'
import { Icon } from '@/components/Icon'
import { TaskName } from '@/components/TaskName'
import { Panel } from '@/components/task/Panel'
import { RecentRuns } from '@/components/workers/RecentRuns'
import { WorkerStatus } from '@/components/workers/WorkerStatus'
import { leadingWorker, registeredTasks, TD, TH } from '@/components/workers/workers'
import { useCarriedRange } from '@/hooks/useCarriedRange'
import { formatAgo, formatDateTime } from '@/utils/format'

export const Route = createFileRoute('/workers_/$hostname')({
  component: WorkerPage,
  staticData: { section: { label: 'Workers', to: '/workers' } },
})

const BUTTON =
  'inline-flex h-[30px] items-center rounded-[3px] bg-raised px-3 text-xs font-semibold whitespace-nowrap hover:bg-press'

function WorkerPage() {
  const { hostname } = Route.useParams()
  // The list, not the per-host route: only the list asks Celery for the current state first
  const { data, isLoading, error } = useWorkers()
  const processes = useMemo(
    () => (data?.workers ?? []).filter((worker) => worker.hostname === hostname),
    [data, hostname],
  )

  if (isLoading) {
    return <p className="m-0 text-xs text-muted">Loading worker...</p>
  }
  if (error) {
    return (
      <p className="rounded-lg bg-surface px-4 py-10 text-center text-xs font-semibold text-failure-t">
        Failed to load worker
      </p>
    )
  }
  if (processes.length === 0) {
    return (
      <div className="rounded-lg bg-surface px-4 py-10 text-center text-xs text-muted">
        <p className="m-0">No worker named {hostname}</p>
        <Link to="/workers" className="mt-2 inline-block underline hover:text-ink">
          Back to workers
        </Link>
      </div>
    )
  }

  const worker = leadingWorker(processes)
  return (
    <div>
      <WorkerHeader worker={worker} />
      <div className="grid grid-cols-1 items-start gap-x-4 xl:grid-cols-[minmax(0,1fr)_280px]">
        <div className="min-w-0">
          <RegisteredTasks tasks={registeredTasks(processes)} />
          <RecentRuns hostname={hostname} />
        </div>
        <Details worker={worker} />
      </div>
    </div>
  )
}

function WorkerHeader({ worker }: { worker: Worker }) {
  const carried = useCarriedRange()
  const offline = worker.status === 'offline'
  return (
    <>
      <div className="mb-4 flex flex-wrap items-center gap-x-3 gap-y-2">
        <h1 className="m-0 min-w-0 font-mono text-2xl font-bold [overflow-wrap:anywhere]">
          {worker.hostname}
        </h1>
        <WorkerStatus status={worker.status} />
        <span className="grow" />
        <Link
          to="/executions"
          search={{ hostname: worker.hostname, ...carried }}
          className={BUTTON}
        >
          All executions on this worker
        </Link>
      </div>
      {offline && (
        <div
          role="note"
          className="mb-4 flex gap-3 rounded-lg bg-[color-mix(in_srgb,var(--failure)_12%,var(--surface))] px-4 py-3.5"
        >
          <span className="inline-flex size-7 flex-none items-center justify-center rounded-[3px] bg-failure text-on-failure">
            <Icon name="x" size={18} className="[stroke-width:2.6]" />
          </span>
          <div>
            <h2 className="m-0 text-sm font-bold">Offline</h2>
            <p className="m-0 text-[13px]">
              Last seen {formatDateTime(worker.last_seen)} ({formatAgo(worker.last_seen)}).
            </p>
          </div>
        </div>
      )}
    </>
  )
}

function RegisteredTasks({ tasks }: { tasks: string[] }) {
  const carried = useCarriedRange()
  const { data: groups } = useGroups()
  // A group column only when some group has one of these tasks
  const groupOf = (name: string): string =>
    groups?.groups.find((group) => group.task_names.includes(name))?.name ?? ''
  const showGroup = tasks.some((name) => groupOf(name) !== '')

  return (
    <Panel
      title="Registered tasks"
      aside={
        <span className="text-xs text-muted">
          {tasks.length} {tasks.length === 1 ? 'task' : 'tasks'}
        </span>
      }
    >
      {tasks.length === 0 ? (
        <p className="m-0 px-4 pt-2 pb-5 text-xs text-muted">No registered tasks.</p>
      ) : (
        <div className="max-h-[420px] overflow-auto">
          <table className="w-full min-w-[320px] border-collapse">
            <thead>
              <tr>
                <th scope="col" className={`${TH} sticky top-0 bg-surface`}>
                  Task
                </th>
                {showGroup && (
                  <th scope="col" className={`${TH} sticky top-0 bg-surface`}>
                    Group
                  </th>
                )}
              </tr>
            </thead>
            <tbody>
              {tasks.map((name) => (
                <tr key={name} className="relative border-t border-rule hover:bg-hover">
                  <td className={TD}>
                    <Link
                      to="/executions"
                      search={{ name_exact: name, ...carried }}
                      aria-label={name}
                      className="block max-w-[420px] after:absolute after:inset-0"
                    >
                      <TaskName name={name} />
                    </Link>
                  </td>
                  {showGroup && <td className={TD}>{groupOf(name)}</td>}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Panel>
  )
}

function Details({ worker }: { worker: Worker }) {
  const facts: [string, string, string][] = [
    ['PID', String(worker.pid), ''],
    ['Started', formatAgo(worker.registered_at), formatDateTime(worker.registered_at)],
    ['Last seen', formatAgo(worker.last_seen), formatDateTime(worker.last_seen)],
  ]
  return (
    <Panel title="Details">
      <dl className="m-0 grid grid-cols-[80px_minmax(0,1fr)] gap-x-3 gap-y-[7px] px-4 pt-2 pb-3.5 text-xs sm:grid-cols-[96px_minmax(0,1fr)]">
        {facts.map(([label, value, title]) => (
          <div key={label} className="contents">
            <dt className="text-muted">{label}</dt>
            <dd className="m-0" title={title || undefined}>
              {label === 'PID' ? <span className="font-mono">{value}</span> : value}
            </dd>
          </div>
        ))}
      </dl>
    </Panel>
  )
}
