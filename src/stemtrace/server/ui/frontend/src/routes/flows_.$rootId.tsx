import { createFileRoute, Link } from '@tanstack/react-router'
import { useGraph } from '@/api/queries'
import { TaskGraph } from '@/components/TaskGraph'

export const Route = createFileRoute('/flows_/$rootId')({
  component: GraphDetailPage,
  staticData: { section: { label: 'Flows', to: '/flows' } },
})

function GraphDetailPage() {
  const { rootId } = Route.useParams()
  const { data, isLoading, error } = useGraph(rootId)

  if (isLoading) {
    return (
      <div className="flex items-center justify-center h-96">
        <div className="animate-spin w-8 h-8 border-2 border-slate-600 border-t-green-500 rounded-full" />
      </div>
    )
  }

  if (error || !data) {
    return (
      <div className="text-center py-12">
        <p className="text-red-400">Failed to load graph</p>
        <Link to="/flows" className="text-blue-400 hover:underline mt-2 inline-block">
          Back to flows
        </Link>
      </div>
    )
  }

  return (
    <div className="space-y-6">
      {/* Graph visualization */}
      <div className="bg-slate-900 rounded-xl border border-slate-800 overflow-hidden">
        <div className="h-[600px]">
          <TaskGraph key={data.root_id} nodes={data.nodes} rootId={data.root_id} />
        </div>
      </div>
    </div>
  )
}
