import { createFileRoute } from '@tanstack/react-router'
import { formatDistanceToNow } from 'date-fns'
import { useWorkers } from '@/api/queries'
import { shortestUniqueSuffixes } from '@/utils/taskNames'

export const Route = createFileRoute('/workers')({
  component: WorkersPage,
})

function WorkersPage() {
  const { data, isLoading, error } = useWorkers()
  // Across every worker listed, so one label means one task on the whole page
  const shortNames = shortestUniqueSuffixes(
    data?.workers.flatMap((worker) => worker.registered_tasks) ?? [],
  )

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-slate-100">Workers</h1>
          <p className="text-sm text-slate-400 mt-1">
            Active Celery workers ({data?.total ?? 0} workers)
          </p>
        </div>
      </div>

      {/* Loading state */}
      {isLoading && (
        <div className="space-y-3">
          {[...Array(3)].map((_, i) => (
            <div
              key={i}
              className="bg-slate-900 rounded-xl border border-slate-800 p-4 animate-pulse"
            >
              <div className="h-5 w-64 bg-slate-800 rounded" />
              <div className="h-4 w-48 bg-slate-800 rounded mt-2" />
            </div>
          ))}
        </div>
      )}

      {/* Error state */}
      {error && (
        <div className="text-center py-12">
          <p className="text-red-400">Failed to load workers</p>
        </div>
      )}

      {/* Worker list */}
      {data && data.workers.length > 0 && (
        <div className="space-y-3">
          {data.workers.map((worker) => (
            <WorkerCard
              key={`${worker.hostname}:${worker.pid}`}
              worker={worker}
              shortNames={shortNames}
            />
          ))}
        </div>
      )}

      {/* Empty state */}
      {data && data.workers.length === 0 && (
        <div className="text-center py-12 bg-slate-900 rounded-xl border border-slate-800">
          <svg
            aria-hidden="true"
            className="w-12 h-12 text-slate-600 mx-auto mb-4"
            fill="none"
            stroke="currentColor"
            viewBox="0 0 24 24"
          >
            <path
              strokeLinecap="round"
              strokeLinejoin="round"
              strokeWidth={1.5}
              d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z"
            />
          </svg>
          <p className="text-slate-400">No workers registered</p>
          <p className="text-sm text-slate-500 mt-1">
            Start a Celery worker with stemtrace initialized to see it here
          </p>
        </div>
      )}
    </div>
  )
}

interface WorkerCardProps {
  /** Short label per task name, unique across the workers listed. */
  shortNames: Map<string, string>
  worker: {
    hostname: string
    pid: number
    registered_tasks: string[]
    status: 'online' | 'offline'
    registered_at: string
    last_seen: string
  }
}

function WorkerCard({ worker, shortNames }: WorkerCardProps) {
  const isOnline = worker.status === 'online'
  const lastSeenDate = new Date(worker.last_seen)
  const lastSeenRelative = formatDistanceToNow(lastSeenDate, { addSuffix: true })

  return (
    <div className="bg-slate-900 rounded-xl border border-slate-800 overflow-hidden">
      <div className="p-4">
        <div className="flex items-start justify-between">
          <div className="flex items-start gap-3 flex-1">
            {/* Status indicator */}
            <div
              className={`w-3 h-3 rounded-full mt-1.5 ${
                isOnline ? 'bg-green-500' : 'bg-slate-600'
              }`}
            />

            {/* Worker info */}
            <div className="flex-1 min-w-0">
              <div className="flex items-center gap-2">
                <h3 className="font-medium text-slate-100">{worker.hostname}</h3>
                <span className="text-xs px-2 py-0.5 rounded bg-slate-800 text-slate-400 font-mono">
                  PID: {worker.pid}
                </span>
                <span
                  className={`text-xs px-2 py-0.5 rounded ${
                    isOnline ? 'bg-green-500/10 text-green-400' : 'bg-slate-800 text-slate-500'
                  }`}
                >
                  {isOnline ? 'Online' : 'Offline'}
                </span>
              </div>

              {/* Metadata */}
              <div className="mt-2 space-y-1">
                <div className="flex items-center gap-4 text-sm text-slate-400">
                  <span>
                    <span className="text-slate-500">Tasks:</span>{' '}
                    <span className="text-slate-300 font-medium">
                      {worker.registered_tasks.length}
                    </span>
                  </span>
                  <span>
                    <span className="text-slate-500">Last seen:</span>{' '}
                    <span className="text-slate-300">{lastSeenRelative}</span>
                  </span>
                </div>
              </div>

              {/* Task list preview */}
              {worker.registered_tasks.length > 0 && (
                <div className="mt-3 pt-3 border-t border-slate-800">
                  <p className="text-xs text-slate-500 mb-2">Registered tasks:</p>
                  <div className="flex flex-wrap gap-2">
                    {worker.registered_tasks.slice(0, 10).map((task) => {
                      return (
                        <span
                          key={task}
                          title={task}
                          className="text-xs px-2 py-1 rounded bg-slate-800 text-slate-400 font-mono"
                        >
                          {shortNames.get(task) ?? task}
                        </span>
                      )
                    })}
                    {worker.registered_tasks.length > 10 && (
                      <span className="text-xs px-2 py-1 rounded bg-slate-800 text-slate-500">
                        +{worker.registered_tasks.length - 10} more
                      </span>
                    )}
                  </div>
                </div>
              )}
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}
