/**
 * Filter controls for task list with date range picker.
 */

import type { DateRange } from 'react-day-picker'
import { formatDateParam, parseDateParam } from '@/utils/format'
import { DateRangePicker } from './DateRangePicker'

interface FiltersProps {
  filters: {
    state: string | undefined
    name: string
    /** Set by a task's "View runs": the whole task name to show. */
    name_exact?: string
    from_date?: string
    to_date?: string
  }
  onFiltersChange: (filters: {
    state: string | undefined
    name: string
    name_exact?: string
    from_date?: string
    to_date?: string
  }) => void
}

const STATES = [
  { value: undefined, label: 'All' },
  { value: 'PENDING', label: 'Pending' },
  { value: 'STARTED', label: 'Started' },
  { value: 'SUCCESS', label: 'Success' },
  { value: 'FAILURE', label: 'Failure' },
  { value: 'RETRY', label: 'Retry' },
  { value: 'REVOKED', label: 'Revoked' },
  { value: 'REJECTED', label: 'Rejected' },
  { value: 'IGNORED', label: 'Ignored' },
]

export function Filters({ filters, onFiltersChange }: FiltersProps) {
  const dateRange: DateRange | undefined =
    filters.from_date || filters.to_date
      ? {
          from: parseDateParam(filters.from_date),
          to: parseDateParam(filters.to_date),
        }
      : undefined

  const handleDateChange = (range: DateRange | undefined) => {
    onFiltersChange({
      ...filters,
      from_date: formatDateParam(range?.from),
      to_date: formatDateParam(range?.to),
    })
  }

  const hasActiveFilters =
    filters.state || filters.name || filters.name_exact || filters.from_date || filters.to_date

  return (
    <div className="flex flex-wrap items-center gap-4 p-4 bg-slate-900/50 rounded-lg border border-slate-800">
      {/* State filter */}
      <div className="flex items-center gap-2">
        <span className="text-sm text-slate-400">State:</span>
        <div className="flex gap-1">
          {STATES.map((state) => (
            <button
              type="button"
              key={state.value ?? 'all'}
              onClick={() => onFiltersChange({ ...filters, state: state.value })}
              className={`px-2 py-1 text-xs rounded transition-colors ${
                filters.state === state.value
                  ? 'bg-slate-700 text-slate-100'
                  : 'bg-slate-800 text-slate-400 hover:bg-slate-750 hover:text-slate-300'
              }`}
            >
              {state.label}
            </button>
          ))}
        </div>
      </div>

      {/* Date range picker */}
      <div className="flex items-center gap-2">
        <span className="text-sm text-slate-400">Date:</span>
        <DateRangePicker value={dateRange} onChange={handleDateChange} />
      </div>

      {/* Name search */}
      <div className="flex items-center gap-2 flex-1 min-w-[200px]">
        <label htmlFor="task-search" className="text-sm text-slate-400">
          Search:
        </label>
        <input
          id="task-search"
          type="text"
          value={filters.name}
          onChange={(e) => onFiltersChange({ ...filters, name: e.target.value })}
          placeholder="Filter by task name..."
          className="flex-1 px-3 py-1.5 text-sm bg-slate-800 border border-slate-700 rounded-lg text-slate-200 placeholder-slate-500 focus:outline-none focus:border-slate-600"
        />
      </div>

      {/* Exact task name, from the registry's "View runs" */}
      {filters.name_exact && (
        <span className="flex items-center gap-1.5 text-xs px-2 py-1 rounded bg-slate-800 text-slate-300">
          <span className="text-slate-500">Task:</span>
          <span className="font-mono">{filters.name_exact}</span>
          <button
            type="button"
            aria-label="Remove task filter"
            onClick={() => onFiltersChange({ ...filters, name_exact: undefined })}
            className="text-slate-500 hover:text-slate-300"
          >
            ×
          </button>
        </span>
      )}

      {/* Clear filters */}
      {hasActiveFilters && (
        <button
          type="button"
          onClick={() =>
            onFiltersChange({
              state: undefined,
              name: '',
              name_exact: undefined,
              from_date: undefined,
              to_date: undefined,
            })
          }
          className="text-xs text-slate-500 hover:text-slate-300"
        >
          Clear
        </button>
      )}
    </div>
  )
}
