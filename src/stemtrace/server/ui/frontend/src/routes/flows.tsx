import { createFileRoute } from '@tanstack/react-router'
import { useEffect } from 'react'
import { useGraphsInfinite } from '@/api/queries'
import { FlowRow } from '@/components/flows/FlowRow'
import { carryRange } from '@/hooks/useCarriedRange'
import { useInfiniteScroll } from '@/hooks/useInfiniteScroll'
import { formatCount } from '@/utils/format'
import { flowsSearch, hasDates, withinOf } from './-search'

export const Route = createFileRoute('/flows')({
  component: FlowsPage,
  validateSearch: flowsSearch,
  staticData: { crumb: 'Flows', timeRange: true },
})

const WINDOW_WORDS: Record<string, string> = { '1h': 'the last hour', '6h': 'the last 6h' }

/** " in the last 6h"; empty when no window applies (All, or explicit dates). */
const inWindow = (within: string | undefined): string =>
  within ? ` in ${WINDOW_WORDS[within]}` : ''

function FlowsPage() {
  const { range, from_date, to_date } = Route.useSearch()
  const dates = { from_date, to_date }
  const within = withinOf(range, dates)

  // Dates set the window without touching the range, so the range stays what it was
  const datesSet = hasDates(dates)
  useEffect(() => {
    if (!datesSet) carryRange(range)
  }, [datesSet, range])

  const { data, isLoading, error, hasNextPage, isFetchingNextPage, fetchNextPage } =
    useGraphsInfinite({ within, ...dates })

  const { lastElementRef } = useInfiniteScroll({ hasNextPage, isFetchingNextPage, fetchNextPage })

  const graphs = data?.pages.flatMap((page) => page.graphs) ?? []
  const total = data?.pages[0]?.total

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold text-ink">Flows</h1>
        <p className="mt-1 min-h-5 text-sm text-muted">
          {total !== undefined &&
            `${formatCount(total)} ${total === 1 ? 'flow' : 'flows'}${inWindow(within)}`}
        </p>
      </div>

      {isLoading ? (
        <FlowListSkeleton />
      ) : error ? (
        <div className="py-12 text-center text-failure-t">Failed to load flows</div>
      ) : graphs.length === 0 ? (
        <p className="rounded-lg bg-surface px-4 py-10 text-center text-muted">
          No flows{inWindow(within)}.
        </p>
      ) : (
        <>
          <section aria-label="Flows" className="overflow-hidden rounded-lg bg-surface">
            {graphs.map((graph, index) => (
              <FlowRow
                key={graph.task_id}
                graph={graph}
                rowRef={index === graphs.length - 1 ? lastElementRef : undefined}
              />
            ))}
          </section>
          {isFetchingNextPage && <p className="text-center text-muted">Loading more flows...</p>}
        </>
      )}
    </div>
  )
}

function FlowListSkeleton() {
  return (
    <div aria-hidden="true" className="animate-pulse overflow-hidden rounded-lg bg-surface">
      {[0, 1, 2].map((i) => (
        <div key={i} className="space-y-2 border-t border-rule px-4 py-3 first:border-t-0">
          <div className="h-4 w-48 rounded-[3px] bg-raised" />
          <div className="h-3 w-64 rounded-[3px] bg-raised" />
        </div>
      ))}
    </div>
  )
}
