import type { QueryClient } from '@tanstack/react-query'
import { createRootRouteWithContext, Link, Outlet } from '@tanstack/react-router'
import { TanStackRouterDevtools } from '@tanstack/router-devtools'
import { useWebSocket } from '@/hooks/useWebSocket'

interface RouterContext {
  queryClient: QueryClient
}

export const Route = createRootRouteWithContext<RouterContext>()({
  component: RootLayout,
})

function RootLayout() {
  // Connect to WebSocket for real-time updates
  const { connectionStatus } = useWebSocket()

  return (
    <div className="min-h-screen bg-slate-950">
      {/* Header */}
      <header className="border-b border-slate-800 bg-slate-900/50 backdrop-blur sticky top-0 z-50">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
          <div className="flex items-center justify-between h-14">
            {/* Logo */}
            <Link to="/" className="flex items-center gap-2">
              <span className="text-lg font-bold bg-gradient-to-r from-green-400 to-emerald-500 bg-clip-text text-transparent">
                celery-flow
              </span>
            </Link>

            {/* Navigation */}
            <nav className="flex items-center gap-6">
              <Link
                to="/"
                className="text-sm text-slate-400 hover:text-slate-100 transition-colors [&.active]:text-slate-100"
              >
                Tasks
              </Link>
              <Link
                to="/graphs"
                className="text-sm text-slate-400 hover:text-slate-100 transition-colors [&.active]:text-slate-100"
              >
                Graphs
              </Link>
            </nav>

            {/* Status indicator */}
            <div className="flex items-center gap-2">
              <span
                className={`w-2 h-2 rounded-full ${
                  connectionStatus === 'connected'
                    ? 'bg-green-500'
                    : connectionStatus === 'connecting'
                      ? 'bg-amber-500 animate-pulse'
                      : 'bg-red-500'
                }`}
              />
              <span className="text-xs text-slate-500">
                {connectionStatus === 'connected' ? 'Live' : connectionStatus}
              </span>
            </div>
          </div>
        </div>
      </header>

      {/* Main content */}
      <main className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
        <Outlet />
      </main>

      {/* Dev tools */}
      {import.meta.env.DEV && <TanStackRouterDevtools position="bottom-right" />}
    </div>
  )
}
