import { createFileRoute, Link } from '@tanstack/react-router'
import { useGraph } from '@/api/queries'
import { TaskGraph } from '@/components/TaskGraph'

export const Route = createFileRoute('/graph/$rootId')({
  component: GraphDetailPage,
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
        <Link to="/graphs" className="text-blue-400 hover:underline mt-2 inline-block">
          Back to graphs
        </Link>
      </div>
    )
  }

  return (
    <div className="space-y-6">
      {/* Breadcrumb */}
      <nav className="flex items-center gap-2 text-sm">
        <Link to="/graphs" className="text-slate-400 hover:text-slate-200">
          Graphs
        </Link>
        <span className="text-slate-600">/</span>
        <span className="text-slate-200 font-mono">{rootId.slice(0, 8)}</span>
      </nav>

      {/* Graph visualization */}
      <div className="bg-slate-900 rounded-xl border border-slate-800 overflow-hidden">
        <div className="h-[600px]">
          <TaskGraph nodes={data.nodes} rootId={data.root_id} />
        </div>
      </div>
    </div>
  )
}
