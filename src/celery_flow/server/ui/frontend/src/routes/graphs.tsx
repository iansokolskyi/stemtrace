import { createFileRoute, Link } from '@tanstack/react-router'
import { useGraphs } from '@/api/queries'
import { TaskStateBadge } from '@/components/TaskStateBadge'

export const Route = createFileRoute('/graphs')({
  component: GraphsPage,
})

function GraphsPage() {
  const { data, isLoading, error } = useGraphs()

  return (
    <div className="space-y-6">
      {/* Page header */}
      <div>
        <h1 className="text-2xl font-bold text-slate-100">Task Graphs</h1>
        <p className="text-sm text-slate-500 mt-1">
          Visualize task execution flows and dependencies
        </p>
      </div>

      {/* Graph list */}
      {isLoading ? (
        <GraphListSkeleton />
      ) : error ? (
        <div className="text-center py-12 text-red-400">Failed to load graphs</div>
      ) : !data?.graphs.length ? (
        <EmptyState />
      ) : (
        <div className="grid gap-4">
          {data.graphs.map((graph) => (
            <Link
              key={graph.task_id}
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
              </div>
            </Link>
          ))}
        </div>
      )}
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
