import { createFileRoute, Link } from '@tanstack/react-router'
import { useState } from 'react'
import { useTaskRegistry } from '@/api/queries'

export const Route = createFileRoute('/registry')({
  component: RegistryPage,
})

function RegistryPage() {
  const [searchQuery, setSearchQuery] = useState('')
  const { data, isLoading, error } = useTaskRegistry(searchQuery || undefined)

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-slate-100">Task Registry</h1>
          <p className="text-sm text-slate-400 mt-1">
            All discovered task definitions ({data?.total ?? 0} tasks)
          </p>
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
            <TaskCard key={task.name} task={task} />
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
  task: {
    name: string
    signature: string | null
    docstring: string | null
    module: string | null
    bound: boolean
  }
}

function TaskCard({ task }: TaskCardProps) {
  const [expanded, setExpanded] = useState(false)
  const shortName = task.name.split('.').pop() || task.name

  return (
    <div className="bg-slate-900 rounded-xl border border-slate-800 overflow-hidden">
      {/* Header */}
      <button
        type="button"
        onClick={() => setExpanded(!expanded)}
        className="w-full p-4 flex items-center justify-between text-left hover:bg-slate-800/50 transition-colors"
      >
        <div className="flex items-center gap-3">
          <div className="w-8 h-8 rounded-lg bg-emerald-500/10 flex items-center justify-center">
            <svg
              aria-hidden="true"
              className="w-4 h-4 text-emerald-400"
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
          <div>
            <h3 className="font-medium text-slate-100">{shortName}</h3>
            <p className="text-sm text-slate-500 font-mono">{task.module || 'unknown'}</p>
          </div>
        </div>
        <div className="flex items-center gap-3">
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

      {/* Expanded content */}
      {expanded && (
        <div className="border-t border-slate-800 p-4 space-y-4">
          {/* Full name */}
          <div>
            <dt className="text-xs text-slate-500 mb-1">Full Name</dt>
            <dd className="font-mono text-sm text-slate-300">{task.name}</dd>
          </div>

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
            <Link
              to="/"
              search={{ name: shortName }}
              className="text-xs px-3 py-1.5 rounded-lg bg-slate-800 text-slate-300 hover:bg-slate-700 transition-colors"
            >
              View runs →
            </Link>
          </div>
        </div>
      )}
    </div>
  )
}
