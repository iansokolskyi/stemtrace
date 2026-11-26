import { createFileRoute, Link } from '@tanstack/react-router'
import { isNotFound } from '@/api/client'
import { useGraph } from '@/api/queries'
import { FlowPage } from '@/components/flow/FlowPage'
import { LoadFailure, Notice } from '@/components/Notice'
import { PageStale } from '@/components/PageStale'
import { Skeleton, SkeletonRegion } from '@/components/Skeleton'
import { useViewState } from '@/hooks/useViewState'

export const Route = createFileRoute('/flows_/$rootId')({
  component: GraphDetailPage,
  staticData: { section: { label: 'Flows', to: '/flows' } },
})

function GraphDetailPage() {
  const { rootId } = Route.useParams()
  const query = useGraph(rootId)
  const view = useViewState(query, `graphs/${rootId}`)
  const { data } = query

  if (view.phase === 'loading') return <FlowSkeleton />

  if (view.phase === 'error' || !data) {
    const back = (
      <Link to="/flows" className="link">
        Back to flows
      </Link>
    )
    if (isNotFound(view.error)) {
      return (
        <Notice action={back}>
          This flow is no longer stored. It may have left the retention window.
        </Notice>
      )
    }
    return <LoadFailure what="the flow" view={view} action={back} />
  }

  return (
    <>
      <PageStale views={[view]} />
      <FlowPage key={data.root_id} nodes={data.nodes} rootId={data.root_id} />
    </>
  )
}

/** The page's own shape while the flow loads: a title bar, the diagram and the timeline. */
function FlowSkeleton() {
  return (
    <div data-testid="flow-skeleton">
      <SkeletonRegion label="flow">
        <Skeleton className="mb-2 h-3 w-40" />
        <Skeleton className="mb-3 h-6 w-64" />
        <Skeleton className="mb-4 h-5 w-80 max-w-full" />
        <Skeleton card className="mb-4 h-64" />
        <Skeleton card className="h-40" />
      </SkeletonRegion>
    </div>
  )
}
