import { createFileRoute, Link } from '@tanstack/react-router'
import { clsx } from 'clsx'
import { useWorkers } from '@/api/queries'
import { LoadFailure } from '@/components/Notice'
import { PageHeader } from '@/components/PageHeader'
import { PageStale } from '@/components/PageStale'
import { TableSkeleton } from '@/components/TableSkeleton'
import { WorkerStatus } from '@/components/workers/WorkerStatus'
import { hostsOf, NUM, registeredTasks, TD, TH } from '@/components/workers/workers'
import { useViewState } from '@/hooks/useViewState'
import { formatAgo } from '@/utils/format'

export const Route = createFileRoute('/workers')({
  component: WorkersPage,
  staticData: { crumb: 'Workers' },
})

// A problem row is tinted like the prototype's: failure at 9%, 15% under the pointer.
const PROBLEM_ROW =
  'bg-[color-mix(in_srgb,var(--failure)_9%,var(--surface))] hover:bg-[color-mix(in_srgb,var(--failure)_15%,var(--surface))]'

function WorkersPage() {
  const query = useWorkers()
  const view = useViewState(query, 'workers')
  const { data } = query
  const hosts = hostsOf(data?.workers ?? [])
  const online = hosts.filter((host) => host.leading.status === 'online').length

  return (
    <div className="space-y-4">
      <PageHeader
        title="Workers"
        summary={hosts.length > 0 && `${online} of ${hosts.length} online`}
      />

      <PageStale views={[view]} className="" />

      {view.phase === 'loading' ? (
        <TableSkeleton label="workers" />
      ) : view.phase === 'error' ? (
        <LoadFailure what="workers" view={view} />
      ) : hosts.length === 0 ? (
        <div className="rounded-lg bg-surface px-4 py-10 text-center text-small text-muted">
          <p className="m-0">No workers registered</p>
          <p className="m-0 mt-1">
            Start a Celery worker with stemtrace initialized to see it here.
          </p>
        </div>
      ) : (
        <div className="overflow-x-auto rounded-lg bg-surface">
          <table aria-label="Workers" className="w-full min-w-[560px] border-collapse">
            <thead>
              <tr>
                <th scope="col" className={TH}>
                  Hostname
                </th>
                <th scope="col" className={TH}>
                  Status
                </th>
                <th scope="col" className={clsx(TH, NUM)}>
                  Registered tasks
                </th>
                <th scope="col" className={clsx(TH, NUM)}>
                  Started
                </th>
                <th scope="col" className={clsx(TH, NUM)}>
                  Last seen
                </th>
              </tr>
            </thead>
            <tbody>
              {hosts.map(({ hostname, processes, leading: worker }) => {
                const offline = worker.status === 'offline'
                return (
                  <tr
                    key={hostname}
                    data-problem={offline || undefined}
                    className={clsx(
                      'relative border-b border-rule last:border-b-0',
                      offline ? PROBLEM_ROW : 'hover:bg-hover',
                    )}
                  >
                    <td className={TD}>
                      <Link
                        to="/workers/$hostname"
                        params={{ hostname }}
                        className="font-mono font-bold after:absolute after:inset-0"
                      >
                        {hostname}
                      </Link>
                    </td>
                    <td className={TD}>
                      <WorkerStatus status={worker.status} />
                    </td>
                    <td className={clsx(TD, NUM)}>{registeredTasks(processes).length}</td>
                    <td className={clsx(TD, NUM, 'text-muted')}>
                      {formatAgo(worker.registered_at)}
                    </td>
                    <td className={clsx(TD, NUM, 'text-muted')}>{formatAgo(worker.last_seen)}</td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
