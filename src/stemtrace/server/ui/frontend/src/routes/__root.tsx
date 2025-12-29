import type { QueryClient } from '@tanstack/react-query'
import { createRootRouteWithContext, Link, Outlet } from '@tanstack/react-router'
import { TanStackRouterDevtools } from '@tanstack/router-devtools'
import { useHealth } from '@/api/queries'
import { Tooltip } from '@/components/Tooltip'
import { useWebSocketContext } from '@/hooks/WebSocketContext'

interface RouterContext {
  queryClient: QueryClient
}

export const Route = createRootRouteWithContext<RouterContext>()({
  component: RootLayout,
})

function RootLayout() {
  // Get WebSocket connection status from context
  const { connectionStatus, isConnected } = useWebSocketContext()

  // Check if server is reachable via health endpoint
  const { data: health, isError: isServerDown } = useHealth()

  // Determine display status
  // Priority: Server down > WS connected > WS connecting > Polling
  let statusLabel: string
  let statusColor: string
  let statusTooltip: string

  if (isServerDown) {
    statusLabel = 'Offline'
    statusColor = 'bg-red-500'
    statusTooltip = 'Server is unreachable. Check if stemtrace is running.'
  } else if (isConnected) {
    statusLabel = 'Live'
    statusColor = 'bg-green-500'
    statusTooltip = 'Real-time updates via WebSocket'
  } else if (connectionStatus === 'connecting') {
    statusLabel = 'Connecting'
    statusColor = 'bg-amber-500 animate-pulse'
    statusTooltip = 'Connecting to WebSocket...'
  } else {
    statusLabel = 'Polling'
    statusColor = 'bg-amber-500'
    statusTooltip = 'Updates every 5s. For real-time updates, configure WebSocket proxy.'
  }

  return (
    <div className="min-h-screen bg-slate-950">
      {/* Header */}
      <header className="border-b border-slate-800 bg-slate-900/50 backdrop-blur sticky top-0 z-50">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
          <div className="flex items-center justify-between h-14">
            {/* Logo */}
            <Link to="/" className="flex items-baseline gap-2">
              <span className="text-lg font-bold bg-gradient-to-r from-green-400 to-emerald-500 bg-clip-text text-transparent">
                stemtrace
              </span>
              {health?.version && <span className="text-xs text-slate-500">v{health.version}</span>}
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
              <Link
                to="/registry"
                className="text-sm text-slate-400 hover:text-slate-100 transition-colors [&.active]:text-slate-100"
              >
                Registry
              </Link>
            </nav>

            {/* Status indicator with tooltip */}
            <Tooltip content={statusTooltip} placement="bottom">
              <div className="flex items-center gap-2 cursor-help">
                <span className={`w-2 h-2 rounded-full ${statusColor}`} />
                <span className="text-xs text-slate-500">{statusLabel}</span>
              </div>
            </Tooltip>
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
