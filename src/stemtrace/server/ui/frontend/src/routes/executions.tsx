import { createFileRoute, useNavigate } from '@tanstack/react-router'
import { useState } from 'react'
import { DensityToggle } from '@/components/executions/DensityToggle'
import { ExecutionsTable } from '@/components/executions/ExecutionsTable'
import { Filters } from '@/components/Filters'
import { TaskTimeline } from '@/components/TaskTimeline'
import { useDensity } from '@/hooks/useDensity'
import { executionsSearch } from './-search'

export const Route = createFileRoute('/executions')({
  component: TasksPage,
  validateSearch: executionsSearch,
  staticData: { crumb: 'Executions' },
})

type ViewMode = 'list' | 'timeline'

function TasksPage() {
  const navigate = useNavigate()
  const { state, name, name_exact, from_date, to_date, view } = Route.useSearch()

  const [density, setDensity] = useDensity()
  const [viewMode, setViewMode] = useState<ViewMode>(view ?? 'list')
  const filters = {
    state,
    name: name ?? '',
    name_exact,
    from_date,
    to_date,
  }

  const setFilters = (newFilters: {
    state?: string
    name: string
    name_exact?: string
    from_date?: string
    to_date?: string
  }) => {
    navigate({
      to: '/executions',
      search: {
        state: newFilters.state,
        name: newFilters.name || undefined,
        name_exact: newFilters.name_exact,
        from_date: newFilters.from_date,
        to_date: newFilters.to_date,
        view: viewMode,
      },
      replace: true,
    })
  }

  const handleViewModeChange = (mode: ViewMode) => {
    setViewMode(mode)
    navigate({
      to: '/executions',
      search: {
        state,
        name: name || undefined,
        name_exact,
        from_date,
        to_date,
        view: mode,
      },
      replace: true,
    })
  }

  return (
    <div className="space-y-6">
      {/* Page header */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold text-ink">Tasks</h1>
          <p className="mt-1 text-sm text-muted">Monitor your Celery task execution in real-time</p>
        </div>

        <div className="flex flex-wrap items-center gap-3">
          {viewMode === 'list' && <DensityToggle value={density} onChange={setDensity} />}
          {/* View toggle */}
          <fieldset
            aria-label="View"
            className="m-0 min-w-0 border-0 inline-flex flex-none gap-0.5 rounded-[3px] bg-raised p-0.5"
          >
            {(['list', 'timeline'] as const).map((mode) => (
              <button
                key={mode}
                type="button"
                aria-pressed={viewMode === mode}
                onClick={() => handleViewModeChange(mode)}
                className="h-[26px] rounded-[2px] px-[11px] text-xs font-semibold text-muted hover:bg-hover hover:text-ink aria-pressed:bg-ink aria-pressed:text-on-ink aria-pressed:hover:bg-ink aria-pressed:hover:text-on-ink"
              >
                {mode === 'list' ? 'List' : 'Timeline'}
              </button>
            ))}
          </fieldset>
        </div>
      </div>

      {/* Filters */}
      <Filters filters={filters} onFiltersChange={setFilters} />

      {/* Content */}
      {viewMode === 'list' ? (
        <ExecutionsTable
          params={{
            state: filters.state,
            name: filters.name || undefined,
            name_exact: filters.name_exact,
            from_date: filters.from_date,
            to_date: filters.to_date,
          }}
          density={density}
        />
      ) : (
        <TaskTimeline filters={filters} />
      )}
    </div>
  )
}
