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
import { executionsSearch, withinOf } from './-search'

export const Route = createFileRoute('/executions')({
  component: TasksPage,
  validateSearch: executionsSearch,
  staticData: { crumb: 'Executions', timeRange: true },
})

type ViewMode = 'list' | 'timeline'

const WINDOW_WORDS: Record<string, string> = { '1h': 'the last hour', '6h': 'the last 6h' }

/** "1,204 runs in the last 6h"; no window (All, or explicit dates) is just the count. */
function runsSummary(total: number, within: string | undefined): string {
  const runs = `${total.toLocaleString('en-US')} ${total === 1 ? 'run' : 'runs'}`
  const window = within ? WINDOW_WORDS[within] : undefined
  return window ? `${runs} in ${window}` : runs
}

function TasksPage() {
  const navigate = useNavigate()
  const { view, range, ...filters } = Route.useSearch()
  const viewMode: ViewMode = view ?? 'list'
  // What the API is asked: the filters plus the window the range sets (explicit dates win)
  const params = { ...filters, within: withinOf(range, filters) }

  const [density, setDensity] = useDensity()

  // Suggestions come from rows already loaded for this view (the same query the table or the
  // timeline runs, so no request of its own) and from the groups.
  const listRows = useExecutions(params, { enabled: viewMode === 'list' })
  const timelineRows = useTasks(params, { enabled: viewMode === 'timeline' })
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

  const total = viewMode === 'list' ? listPages?.pages[0]?.total : timelineRows.data?.total

  // Filters are history entries (Back restores the previous ones); the view toggle is not
  const setFilters = (next: ExecutionFilters, { replace }: { replace: boolean }) => {
    // Dates replace the range, so setting any drops it
    const nextRange = next.from_date || next.to_date ? undefined : range
    navigate({ to: '/executions', search: { ...next, view, range: nextRange }, replace })
  }

  const handleViewModeChange = (mode: ViewMode) => {
    navigate({ to: '/executions', search: { ...filters, view: mode, range }, replace: true })
  }

  return (
    <div className="space-y-6">
      {/* Page header */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold text-ink">Executions</h1>
          <p className="mt-1 min-h-5 text-sm text-muted">
            {total !== undefined && runsSummary(total, params.within)}
          </p>
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
        <ExecutionsTable params={params} density={density} />
      ) : (
        <TaskTimeline filters={params} />
      )}
    </div>
  )
}
