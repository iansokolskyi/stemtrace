import { createFileRoute } from '@tanstack/react-router'
import { useGraphsInfinite } from '@/api/queries'
import { FlowRow } from '@/components/flows/FlowRow'
import { LoadFailure, MoreFailed } from '@/components/Notice'
import { PageStale } from '@/components/PageStale'
import { Skeleton, SkeletonRegion } from '@/components/Skeleton'
import { useInfiniteScroll } from '@/hooks/useInfiniteScroll'
import { useNextPage } from '@/hooks/useNextPage'
import { useViewState } from '@/hooks/useViewState'
import { formatCount } from '@/utils/format'
import { flowsSearch, hasDates } from './-search'

export const Route = createFileRoute('/flows')({
  component: FlowsPage,
  validateSearch: flowsSearch,
  staticData: { crumb: 'Flows' },
})

function FlowsPage() {
  const { from_date, to_date } = Route.useSearch()
  const dates = { from_date, to_date }

  const query = useGraphsInfinite(dates)
  const { data, hasNextPage, isFetchingNextPage, fetchNextPage } = query
  const listKey = JSON.stringify(dates)
  const view = useViewState(query, `graphs/${listKey}`)
  const next = useNextPage(query, listKey)

  // A failed next page is asked again by its Retry, not by scrolling
  const { lastElementRef } = useInfiniteScroll({
    hasNextPage: hasNextPage && !next.failed,
    isFetchingNextPage,
    fetchNextPage,
  })

  const graphs = data?.pages.flatMap((page) => page.graphs) ?? []
  const total = data?.pages[0]?.total

  return (
    <div className="space-y-6">
      <div>
        <h1 tabIndex={-1} className="text-2xl font-bold text-ink">
          Flows
        </h1>
        <p className="mt-1 min-h-5 text-sm text-muted">
          {total !== undefined &&
            `${formatCount(total)} ${total === 1 ? 'flow' : 'flows'} ${hasDates(dates) ? 'in this time window' : 'in the retention window'}`}
        </p>
      </div>

      <PageStale views={[view]} className="" />

      {view.phase === 'loading' ? (
        <FlowListSkeleton />
      ) : view.phase === 'error' ? (
        <LoadFailure what="flows" view={view} />
      ) : graphs.length === 0 ? (
        <p className="rounded-lg bg-surface px-4 py-10 text-center text-muted">
          {hasDates(dates) ? 'No flows in this time window.' : 'No flows yet.'}
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
          {next.failed && <MoreFailed error={next.error} retry={next.retry} busy={next.busy} />}
        </>
      )}
    </div>
  )
}

/** Three rows with a title line and a detail line, like a flow row. */
function FlowListSkeleton() {
  return (
    <SkeletonRegion label="flows" className="overflow-hidden rounded-lg bg-surface">
      {[0, 1, 2].map((i) => (
        <div key={i} className="space-y-2 border-t border-rule px-4 py-3 first:border-t-0">
          <Skeleton className="h-4 w-48" />
          <Skeleton className="h-3 w-64" />
        </div>
      ))}
    </SkeletonRegion>
  )
}
