import { createFileRoute, Link } from '@tanstack/react-router'
import { clsx } from 'clsx'
import { useWorkers } from '@/api/queries'
import { WorkerStatus } from '@/components/workers/WorkerStatus'
import { NUM, sortedWorkers, TD, TH } from '@/components/workers/workers'
import { formatAgo } from '@/utils/format'

export const Route = createFileRoute('/workers')({
  component: WorkersPage,
  staticData: { crumb: 'Workers' },
})

// A problem row is tinted like the prototype's: failure at 9%, 15% under the pointer.
const PROBLEM_ROW =
  'bg-[color-mix(in_srgb,var(--failure)_9%,var(--surface))] hover:bg-[color-mix(in_srgb,var(--failure)_15%,var(--surface))]'

function WorkersPage() {
  const { data, isLoading, error } = useWorkers()
  const workers = sortedWorkers(data?.workers ?? [])
  const online = workers.filter((worker) => worker.status === 'online').length

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-2xl font-bold text-ink">Workers</h1>
        <p className="mt-1 min-h-5 text-sm text-muted">
          {workers.length > 0 && `${online} of ${workers.length} online`}
        </p>
      </div>

      {isLoading ? (
        <div
          aria-hidden="true"
          className="animate-pulse space-y-px overflow-hidden rounded-lg bg-surface"
        >
          {[0, 1, 2].map((i) => (
            <div key={i} className="h-9 border-t border-rule first:border-t-0" />
          ))}
        </div>
      ) : error ? (
        <p className="rounded-lg bg-surface px-4 py-10 text-center text-xs font-semibold text-failure-t">
          Failed to load workers
        </p>
      ) : workers.length === 0 ? (
        <div className="rounded-lg bg-surface px-4 py-10 text-center text-xs text-muted">
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
              {workers.map((worker) => {
                const offline = worker.status === 'offline'
                // One host can have several processes: tell them apart by pid
                const shared = workers.filter((w) => w.hostname === worker.hostname).length > 1
                return (
                  <tr
                    key={`${worker.hostname}:${worker.pid}`}
                    data-problem={offline || undefined}
                    className={clsx(
                      'relative border-b border-rule last:border-b-0',
                      offline ? PROBLEM_ROW : 'hover:bg-hover',
                    )}
                  >
                    <td className={TD}>
                      <Link
                        to="/workers/$hostname"
                        params={{ hostname: worker.hostname }}
                        className="font-mono font-bold after:absolute after:inset-0"
                      >
                        {worker.hostname}
                      </Link>
                      {shared && <span className="ml-2 text-muted">pid {worker.pid}</span>}
                    </td>
                    <td className={TD}>
                      <WorkerStatus status={worker.status} />
                    </td>
                    <td className={clsx(TD, NUM)}>{worker.registered_tasks.length}</td>
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
