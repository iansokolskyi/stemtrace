import { createFileRoute, Link, useNavigate } from '@tanstack/react-router'
import type { DateRange } from 'react-day-picker'
import { useGraphsInfinite } from '@/api/queries'
import { DateRangePicker } from '@/components/DateRangePicker'
import { TaskStateBadge } from '@/components/TaskStateBadge'
import { useInfiniteScroll } from '@/hooks/useInfiniteScroll'
import { formatDateParam, formatDuration, formatTime, parseDateParam } from '@/utils/format'

interface SearchParams {
  from_date?: string
  to_date?: string
}

export const Route = createFileRoute('/graphs')({
  component: GraphsPage,
  validateSearch: (search: Record<string, unknown>): SearchParams => ({
    from_date: typeof search.from_date === 'string' ? search.from_date : undefined,
    to_date: typeof search.to_date === 'string' ? search.to_date : undefined,
  }),
})

function GraphsPage() {
  const navigate = useNavigate()
  const { from_date, to_date } = Route.useSearch()

  const { data, isLoading, error, hasNextPage, isFetchingNextPage, fetchNextPage } =
    useGraphsInfinite({
      from_date,
      to_date,
    })

  const { lastElementRef } = useInfiniteScroll({
    hasNextPage,
    isFetchingNextPage,
    fetchNextPage,
  })

  const dateRange: DateRange | undefined =
    from_date || to_date
      ? {
          from: parseDateParam(from_date),
          to: parseDateParam(to_date),
        }
      : undefined

  const handleDateChange = (range: DateRange | undefined) => {
    navigate({
      to: '/graphs',
      search: {
        from_date: formatDateParam(range?.from),
        to_date: formatDateParam(range?.to),
      },
      replace: true,
    })
  }

  const allGraphs = data?.pages.flatMap((page) => page.graphs) ?? []
  const total = data?.pages[0]?.total ?? 0

  return (
    <div className="space-y-6">
      <div className="flex items-start justify-between">
        <div>
          <h1 className="text-2xl font-bold text-slate-100">Task Graphs</h1>
          <p className="text-sm text-slate-500 mt-1">
            Visualize task execution flows and dependencies
          </p>
        </div>
        <DateRangePicker value={dateRange} onChange={handleDateChange} />
      </div>

      {isLoading ? (
        <GraphListSkeleton />
      ) : error ? (
        <div className="text-center py-12 text-red-400">Failed to load graphs</div>
      ) : !allGraphs.length ? (
        <EmptyState />
      ) : (
        <div className="grid gap-4">
          {allGraphs.map((graph, index) => {
            const isLast = index === allGraphs.length - 1
            const startTime = formatTime(graph.first_seen)
            const duration = formatDuration(graph.duration_ms)

            return (
              <Link
                key={graph.task_id}
                ref={isLast ? lastElementRef : undefined}
                to="/graph/$rootId"
                params={{ rootId: graph.task_id }}
                className="bg-slate-900 rounded-xl border border-slate-800 p-6 hover:border-slate-700 transition-colors"
              >
                <div className="flex items-start justify-between">
                  <div>
                    <h3 className="text-lg font-medium text-slate-100">{graph.name}</h3>
                    <p className="text-sm text-slate-500 font-mono mt-1">{graph.task_id}</p>
                  </div>
                  <TaskStateBadge state={graph.state} />
                </div>

                <div className="flex items-center gap-4 mt-4 text-sm text-slate-400">
                  <span>{graph.children.length} child tasks</span>
                  {startTime && (
                    <>
                      <span className="text-slate-600">·</span>
                      <span>{startTime}</span>
                    </>
                  )}
                  {duration && (
                    <>
                      <span className="text-slate-600">·</span>
                      <span>{duration}</span>
                    </>
                  )}
                </div>
              </Link>
            )
          })}

          {/* Loading indicator */}
          {isFetchingNextPage && <LoadingMore />}

          {/* Pagination info */}
          <div className="text-center text-sm text-slate-500 pt-4">
            Showing {allGraphs.length} of {total} graphs
          </div>
        </div>
      )}
    </div>
  )
}

function LoadingMore() {
  return (
    <div className="flex justify-center py-4">
      <div className="flex items-center gap-2 text-sm text-slate-400">
        <svg
          className="animate-spin h-4 w-4"
          xmlns="http://www.w3.org/2000/svg"
          fill="none"
          viewBox="0 0 24 24"
          aria-hidden="true"
        >
          <circle
            className="opacity-25"
            cx="12"
            cy="12"
            r="10"
            stroke="currentColor"
            strokeWidth="4"
          />
          <path
            className="opacity-75"
            fill="currentColor"
            d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"
          />
        </svg>
        Loading more...
      </div>
    </div>
  )
}

function EmptyState() {
  return (
    <div className="text-center py-16 bg-slate-900/50 rounded-xl border border-slate-800">
      <div className="text-4xl mb-4">🌱</div>
      <h3 className="text-lg font-medium text-slate-200">No task graphs yet</h3>
      <p className="text-sm text-slate-500 mt-1">Run some Celery tasks to see them appear here</p>
    </div>
  )
}

function GraphListSkeleton() {
  return (
    <div className="grid gap-4 animate-pulse">
      {[...Array(3)].map((_, i) => (
        <div key={i} className="bg-slate-900 rounded-xl border border-slate-800 p-6">
          <div className="h-5 w-48 bg-slate-800 rounded" />
          <div className="h-4 w-64 bg-slate-800 rounded mt-2" />
          <div className="h-4 w-24 bg-slate-800 rounded mt-4" />
        </div>
      ))}
    </div>
  )
}
