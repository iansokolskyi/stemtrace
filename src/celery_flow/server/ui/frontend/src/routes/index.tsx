import { createFileRoute, useNavigate } from '@tanstack/react-router'
import { useState } from 'react'
import { Filters } from '@/components/Filters'
import { TaskList } from '@/components/TaskList'
import { TaskTimeline } from '@/components/TaskTimeline'

interface SearchParams {
  state?: string
  name?: string
  view?: 'list' | 'timeline'
}

export const Route = createFileRoute('/')({
  component: TasksPage,
  validateSearch: (search: Record<string, unknown>): SearchParams => ({
    state: typeof search.state === 'string' ? search.state : undefined,
    name: typeof search.name === 'string' ? search.name : undefined,
    view: search.view === 'list' || search.view === 'timeline' ? search.view : undefined,
  }),
})

type ViewMode = 'list' | 'timeline'

function TasksPage() {
  const navigate = useNavigate()
  const { state, name, view } = Route.useSearch()

  const [viewMode, setViewMode] = useState<ViewMode>(view ?? 'list')
  const filters = {
    state,
    name: name ?? '',
  }

  const setFilters = (newFilters: { state?: string; name: string }) => {
    navigate({
      to: '/',
      search: {
        state: newFilters.state,
        name: newFilters.name || undefined,
        view: viewMode,
      },
      replace: true,
    })
  }

  const handleViewModeChange = (mode: ViewMode) => {
    setViewMode(mode)
    navigate({
      to: '/',
      search: {
        state,
        name: name || undefined,
        view: mode,
      },
      replace: true,
    })
  }

  return (
    <div className="space-y-6">
      {/* Page header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-slate-100">Tasks</h1>
          <p className="text-sm text-slate-500 mt-1">
            Monitor your Celery task execution in real-time
          </p>
        </div>

        {/* View toggle */}
        <div className="flex items-center gap-2 bg-slate-800 rounded-lg p-1">
          <button
            type="button"
            onClick={() => handleViewModeChange('list')}
            className={`px-3 py-1.5 text-sm rounded-md transition-colors ${
              viewMode === 'list'
                ? 'bg-slate-700 text-slate-100'
                : 'text-slate-400 hover:text-slate-200'
            }`}
          >
            List
          </button>
          <button
            type="button"
            onClick={() => handleViewModeChange('timeline')}
            className={`px-3 py-1.5 text-sm rounded-md transition-colors ${
              viewMode === 'timeline'
                ? 'bg-slate-700 text-slate-100'
                : 'text-slate-400 hover:text-slate-200'
            }`}
          >
            Timeline
          </button>
        </div>
      </div>

      {/* Filters */}
      <Filters filters={filters} onFiltersChange={setFilters} />

      {/* Content */}
      {viewMode === 'list' ? <TaskList filters={filters} /> : <TaskTimeline filters={filters} />}
    </div>
  )
}
