/**
 * Filter controls for task list.
 */

interface FiltersProps {
  filters: {
    state: string | undefined
    name: string
  }
  onFiltersChange: (filters: { state: string | undefined; name: string }) => void
}

const STATES = [
  { value: undefined, label: 'All' },
  { value: 'PENDING', label: 'Pending' },
  { value: 'STARTED', label: 'Started' },
  { value: 'SUCCESS', label: 'Success' },
  { value: 'FAILURE', label: 'Failure' },
  { value: 'RETRY', label: 'Retry' },
  { value: 'REVOKED', label: 'Revoked' },
]

export function Filters({ filters, onFiltersChange }: FiltersProps) {
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
              className={`px-2 py-1 text-xs rounded transition-colors ${filters.state === state.value
                  ? 'bg-slate-700 text-slate-100'
                  : 'bg-slate-800 text-slate-400 hover:bg-slate-750 hover:text-slate-300'
                }`}
            >
              {state.label}
            </button>
          ))}
        </div>
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

      {/* Clear filters */}
      {(filters.state || filters.name) && (
        <button
          type="button"
          onClick={() => onFiltersChange({ state: undefined, name: '' })}
          className="text-xs text-slate-500 hover:text-slate-300"
        >
          Clear
        </button>
      )}
    </div>
  )
}
