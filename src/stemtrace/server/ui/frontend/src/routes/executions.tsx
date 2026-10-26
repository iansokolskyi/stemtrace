import { createFileRoute, useNavigate } from '@tanstack/react-router'
import { useMemo } from 'react'
import type { ExecutionFilters } from '@/api/client'
import { useExecutions, useGroups, useTasks } from '@/api/queries'
import { DensityToggle } from '@/components/executions/DensityToggle'
import { ExecutionsTable } from '@/components/executions/ExecutionsTable'
import { FilterChips } from '@/components/executions/FilterChips'
import { suggestionsFrom } from '@/components/executions/suggestions'
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
  const { view, ...filters } = Route.useSearch()
  const viewMode: ViewMode = view ?? 'list'

  const [density, setDensity] = useDensity()

  // Suggestions come from rows already loaded for this view (the same query the table or the
  // timeline runs, so no request of its own) and from the groups.
  const listRows = useExecutions(filters, { enabled: viewMode === 'list' })
  const timelineRows = useTasks(filters, { enabled: viewMode === 'timeline' })
  const { data: groups } = useGroups()
  const listPages = listRows.data
  const timelineTasks = timelineRows.data?.tasks
  const groupList = groups?.groups
  const suggestions = useMemo(
    () =>
      suggestionsFrom(
        viewMode === 'list'
          ? (listPages?.pages.flatMap((page) => page.tasks) ?? [])
          : (timelineTasks ?? []),
        groupList,
      ),
    [viewMode, listPages, timelineTasks, groupList],
  )

  // Filters are history entries (Back restores the previous ones); the view toggle is not
  const setFilters = (next: ExecutionFilters, { replace }: { replace: boolean }) => {
    navigate({ to: '/executions', search: { ...next, view }, replace })
  }

  const handleViewModeChange = (mode: ViewMode) => {
    navigate({ to: '/executions', search: { ...filters, view: mode }, replace: true })
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

      <FilterChips filters={filters} onChange={setFilters} suggestions={suggestions} />

      {viewMode === 'list' ? (
        <ExecutionsTable params={filters} density={density} />
      ) : (
        <TaskTimeline filters={filters} />
      )}
    </div>
  )
}
