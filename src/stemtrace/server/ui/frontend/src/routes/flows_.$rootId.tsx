import { createFileRoute, Link } from '@tanstack/react-router'
import { useGraph } from '@/api/queries'
import { FlowPage } from '@/components/flow/FlowPage'

export const Route = createFileRoute('/flows_/$rootId')({
  component: GraphDetailPage,
  staticData: { section: { label: 'Flows', to: '/flows' } },
})

function GraphDetailPage() {
  const { rootId } = Route.useParams()
  const { data, isLoading, error } = useGraph(rootId)

  if (isLoading) return <FlowSkeleton />

  if (error || !data) {
    return (
      <div role="alert" className="rounded-lg bg-surface px-4 py-10 text-center text-xs">
        <p className="m-0 font-semibold text-failure-t">Failed to load flow</p>
        <Link
          to="/flows"
          className="mt-2 inline-block font-semibold text-ink underline underline-offset-2"
        >
          Back to flows
        </Link>
      </div>
    )
  }

  return <FlowPage key={data.root_id} nodes={data.nodes} rootId={data.root_id} />
}

/** The page's own shape while the flow loads: a title bar, the diagram and the timeline, no motion. */
function FlowSkeleton() {
  return (
    <div data-testid="flow-skeleton" aria-hidden="true">
      <div className="mb-2 h-3 w-40 rounded-[3px] bg-raised" />
      <div className="mb-3 h-6 w-64 rounded-[3px] bg-raised" />
      <div className="mb-4 h-5 w-80 max-w-full rounded-[3px] bg-raised" />
      <div className="mb-4 h-64 rounded-lg bg-surface" />
      <div className="h-40 rounded-lg bg-surface" />
    </div>
  )
}
