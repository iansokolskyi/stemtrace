import { createFileRoute, Link } from '@tanstack/react-router'
import { useMemo, useState } from 'react'
import type { TaskStatus } from '@/api/client'
import { useTaskRegistry } from '@/api/queries'
import { useCarriedRange } from '@/hooks/useCarriedRange'
import { shortestUniqueSuffixes } from '@/utils/taskNames'

export const Route = createFileRoute('/registry')({
  component: RegistryPage,
  staticData: { crumb: 'Registry' },
})

type StatusFilter = TaskStatus | undefined

const STATUS_OPTIONS: { value: StatusFilter; label: string }[] = [
  { value: undefined, label: 'All' },
  { value: 'active', label: 'Active' },
  { value: 'never_run', label: 'Never Run' },
  { value: 'not_registered', label: 'Not Registered' },
]

function formatRelativeTime(dateString: string | null): string {
  if (!dateString) return 'Never'
  const date = new Date(dateString)
  const now = new Date()
  const diffMs = now.getTime() - date.getTime()
  const diffSec = Math.floor(diffMs / 1000)
  const diffMin = Math.floor(diffSec / 60)
  const diffHour = Math.floor(diffMin / 60)
  const diffDay = Math.floor(diffHour / 24)

  if (diffSec < 60) return 'Just now'
  if (diffMin < 60) return `${diffMin} min ago`
  if (diffHour < 24) return `${diffHour}h ago`
  if (diffDay === 1) return 'Yesterday'
  if (diffDay < 7) return `${diffDay} days ago`
  return date.toLocaleDateString()
}

function RegistryPage() {
  const [searchQuery, setSearchQuery] = useState('')
  const [statusFilter, setStatusFilter] = useState<StatusFilter>(undefined)
  const { data, isLoading, error } = useTaskRegistry(searchQuery || undefined, statusFilter)
  const shortNames = useMemo(
    () => shortestUniqueSuffixes(data?.tasks.map((task) => task.name) ?? []),
    [data],
  )

  return (
    <div className="space-y-6">
      {/* Header with status dropdown */}
      <div className="flex items-center justify-between flex-wrap gap-4">
        <div>
          <h1 className="text-2xl font-bold text-slate-100">Task Registry</h1>
          <p className="text-sm text-slate-400 mt-1">
            All discovered task definitions ({data?.total ?? 0} tasks)
          </p>
        </div>
        <div className="flex items-center gap-3">
          <label htmlFor="status-filter" className="text-sm text-slate-400">
            Status:
          </label>
          <select
            id="status-filter"
            value={statusFilter ?? ''}
            onChange={(e) => setStatusFilter((e.target.value || undefined) as StatusFilter)}
            className="bg-slate-900 border border-slate-700 rounded-lg px-3 py-2 text-sm text-slate-100 focus:outline-none focus:ring-2 focus:ring-emerald-500/50 focus:border-emerald-500"
          >
            {STATUS_OPTIONS.map((opt) => (
              <option key={opt.label} value={opt.value ?? ''}>
                {opt.label}
              </option>
            ))}
          </select>
        </div>
      </div>

      {/* Search */}
      <div className="relative">
        <input
          type="text"
          placeholder="Search tasks..."
          value={searchQuery}
          onChange={(e) => setSearchQuery(e.target.value)}
          className="w-full bg-slate-900 border border-slate-700 rounded-lg px-4 py-2.5 text-slate-100 placeholder-slate-500 focus:outline-none focus:ring-2 focus:ring-emerald-500/50 focus:border-emerald-500"
        />
        <svg
          aria-hidden="true"
          className="absolute right-3 top-1/2 -translate-y-1/2 w-5 h-5 text-slate-500"
          fill="none"
          stroke="currentColor"
          viewBox="0 0 24 24"
        >
          <path
            strokeLinecap="round"
            strokeLinejoin="round"
            strokeWidth={2}
            d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z"
          />
        </svg>
      </div>

      {/* Loading state */}
      {isLoading && (
        <div className="space-y-3">
          {[...Array(5)].map((_, i) => (
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
          <p className="text-red-400">Failed to load task registry</p>
        </div>
      )}

      {/* Task list */}
      {data && data.tasks.length > 0 && (
        <div className="space-y-3">
          {data.tasks.map((task) => (
            <TaskCard
              key={task.name}
              task={task}
              shortName={shortNames.get(task.name) ?? task.name}
            />
          ))}
        </div>
      )}

      {/* Empty state */}
      {data && data.tasks.length === 0 && (
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
              d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2"
            />
          </svg>
          <p className="text-slate-400">
            {searchQuery ? 'No tasks match your search' : 'No tasks discovered yet'}
          </p>
          <p className="text-sm text-slate-500 mt-1">
            Run some Celery tasks to see them appear here
          </p>
        </div>
      )}
    </div>
  )
}

interface TaskCardProps {
  /** The name as far as it takes to tell it from the other tasks listed. */
  shortName: string
  task: {
    name: string
    signature: string | null
    docstring: string | null
    module: string | null
    bound: boolean
    execution_count: number
    registered_by: string[]
    last_run: string | null
    status: TaskStatus
  }
}

function StatusBadge({ status }: { status: TaskStatus }) {
  const config = {
    active: {
      bg: 'bg-green-500/10',
      text: 'text-green-400',
      dotColor: 'bg-green-400',
      label: 'Active',
    },
    never_run: {
      bg: 'bg-amber-500/10',
      text: 'text-amber-400',
      dotColor: 'bg-amber-400',
      label: 'Never Run',
    },
    not_registered: {
      bg: 'bg-red-500/10',
      text: 'text-red-400',
      dotColor: 'bg-red-400',
      label: 'Not Registered',
    },
  }[status]

  return (
    <span
      className={`inline-flex items-center gap-1.5 text-xs px-2 py-0.5 rounded ${config.bg} ${config.text} flex-shrink-0`}
    >
      <span className={`w-1.5 h-1.5 rounded-full ${config.dotColor}`} />
      {config.label}
    </span>
  )
}

function TaskCard({ task, shortName }: TaskCardProps) {
  const carried = useCarriedRange()
  const [expanded, setExpanded] = useState(false)
  const hasExecutions = task.execution_count > 0

  // Icon colors based on status
  const iconConfig = {
    active: { bg: 'bg-green-500/10', stroke: 'text-green-400' },
    never_run: { bg: 'bg-amber-500/10', stroke: 'text-amber-400' },
    not_registered: { bg: 'bg-red-500/10', stroke: 'text-red-400' },
  }[task.status]

  return (
    <div
      className={`bg-slate-900 rounded-xl border overflow-hidden ${
        task.status === 'not_registered' ? 'border-red-500/30' : 'border-slate-800'
      }`}
    >
      {/* Header */}
      <button
        type="button"
        onClick={() => setExpanded(!expanded)}
        className="w-full p-4 flex items-center justify-between text-left hover:bg-slate-800/50 transition-colors"
      >
        <div className="flex items-center gap-3 flex-1 min-w-0">
          <div
            className={`w-8 h-8 rounded-lg ${iconConfig.bg} flex items-center justify-center flex-shrink-0`}
          >
            <svg
              aria-hidden="true"
              className={`w-4 h-4 ${iconConfig.stroke}`}
              fill="none"
              stroke="currentColor"
              viewBox="0 0 24 24"
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                strokeWidth={2}
                d="M13 10V3L4 14h7v7l9-11h-7z"
              />
            </svg>
          </div>
          <div className="flex-1 min-w-0">
            <div className="flex items-center gap-2 flex-wrap">
              <h3 className="font-medium text-slate-100 truncate" title={task.name}>
                {shortName}
              </h3>
              <StatusBadge status={task.status} />
            </div>
            <p className="text-sm text-slate-500 mt-0.5">
              {hasExecutions ? (
                <>
                  {task.execution_count} {task.execution_count === 1 ? 'run' : 'runs'} · Last run:{' '}
                  {formatRelativeTime(task.last_run)}
                </>
              ) : task.registered_by.length > 0 ? (
                <>Registered by {task.registered_by.join(', ')}</>
              ) : (
                <span className="font-mono">{task.module || 'unknown'}</span>
              )}
            </p>
          </div>
        </div>
        <div className="flex items-center gap-3 flex-shrink-0">
          {task.bound && (
            <span className="text-xs px-2 py-1 rounded bg-slate-800 text-slate-400">bound</span>
          )}
          <svg
            aria-hidden="true"
            className={`w-5 h-5 text-slate-500 transition-transform ${expanded ? 'rotate-180' : ''}`}
            fill="none"
            stroke="currentColor"
            viewBox="0 0 24 24"
          >
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
          </svg>
        </div>
      </button>

      {/* Warning for not registered tasks */}
      {task.status === 'not_registered' && !expanded && (
        <div className="px-4 pb-3 -mt-1">
          <p className="text-xs text-red-400/80 flex items-center gap-1.5">
            <svg
              aria-hidden="true"
              className="w-3.5 h-3.5"
              fill="none"
              stroke="currentColor"
              viewBox="0 0 24 24"
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                strokeWidth={2}
                d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z"
              />
            </svg>
            No worker has this task registered
          </p>
        </div>
      )}

      {/* Expanded content */}
      {expanded && (
        <div className="border-t border-slate-800 p-4 space-y-4">
          {/* Warning message for not registered */}
          {task.status === 'not_registered' && (
            <div className="bg-red-500/5 border border-red-500/20 rounded-lg p-3">
              <p className="text-sm text-red-400 flex items-start gap-2">
                <svg
                  aria-hidden="true"
                  className="w-4 h-4 mt-0.5 flex-shrink-0"
                  fill="none"
                  stroke="currentColor"
                  viewBox="0 0 24 24"
                >
                  <path
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    strokeWidth={2}
                    d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z"
                  />
                </svg>
                <span>
                  <strong>Not Registered:</strong> This task has been executed but no current worker
                  has it registered. New tasks with this name may get stuck in PENDING.
                </span>
              </p>
            </div>
          )}

          {/* Full name */}
          <div>
            <dt className="text-xs text-slate-500 mb-1">Full Name</dt>
            <dd className="font-mono text-sm text-slate-300">{task.name}</dd>
          </div>

          {/* Module */}
          {task.module && (
            <div>
              <dt className="text-xs text-slate-500 mb-1">Module</dt>
              <dd className="font-mono text-sm text-slate-300">{task.module}</dd>
            </div>
          )}

          {/* Execution stats */}
          <div className="grid grid-cols-2 gap-4">
            <div>
              <dt className="text-xs text-slate-500 mb-1">Executions</dt>
              <dd className="text-sm text-slate-300">
                {task.execution_count} {task.execution_count === 1 ? 'run' : 'runs'}
              </dd>
            </div>
            <div>
              <dt className="text-xs text-slate-500 mb-1">Last Run</dt>
              <dd className="text-sm text-slate-300">{formatRelativeTime(task.last_run)}</dd>
            </div>
          </div>

          {/* Registered by workers */}
          {task.registered_by.length > 0 && (
            <div>
              <dt className="text-xs text-slate-500 mb-1">Registered by</dt>
              <dd className="flex flex-wrap gap-2">
                {task.registered_by.map((hostname) => (
                  <span
                    key={hostname}
                    className="text-xs px-2 py-1 rounded bg-slate-800 text-slate-400 font-mono"
                  >
                    {hostname}
                  </span>
                ))}
              </dd>
            </div>
          )}

          {/* Signature */}
          {task.signature && (
            <div>
              <dt className="text-xs text-slate-500 mb-1">Signature</dt>
              <dd className="font-mono text-sm text-emerald-400 bg-slate-950 rounded-lg px-3 py-2">
                {task.signature}
              </dd>
            </div>
          )}

          {/* Docstring */}
          {task.docstring && (
            <div>
              <dt className="text-xs text-slate-500 mb-1">Documentation</dt>
              <dd className="text-sm text-slate-300 whitespace-pre-wrap">{task.docstring}</dd>
            </div>
          )}

          {/* Actions */}
          <div className="flex gap-2 pt-2">
            {hasExecutions && (
              <Link
                to="/executions"
                search={{ name_exact: task.name, ...carried }}
                className="text-xs px-3 py-1.5 rounded-lg bg-slate-800 text-slate-300 hover:bg-slate-700 transition-colors"
              >
                View runs →
              </Link>
            )}
          </div>
        </div>
      )}
    </div>
  )
}
