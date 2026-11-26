import { createFileRoute, Link } from '@tanstack/react-router'
import { useGraph } from '@/api/queries'
import { FlowPage } from '@/components/flow/FlowPage'
import { LoadFailure } from '@/components/Notice'
import { Skeleton, SkeletonRegion } from '@/components/Skeleton'

export const Route = createFileRoute('/flows_/$rootId')({
  component: GraphDetailPage,
  staticData: { section: { label: 'Flows', to: '/flows' } },
})

function GraphDetailPage() {
  const { rootId } = Route.useParams()
  const { data, isLoading, error, refetch } = useGraph(rootId)

  if (isLoading) return <FlowSkeleton />

  if (error || !data) {
    return (
      <LoadFailure
        what="the flow"
        error={error}
        onRetry={refetch}
        action={
          <Link to="/flows" className="link">
            Back to flows
          </Link>
        }
      />
    )
  }

  return <FlowPage key={data.root_id} nodes={data.nodes} rootId={data.root_id} />
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
