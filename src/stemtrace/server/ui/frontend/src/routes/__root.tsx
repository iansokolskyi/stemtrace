import type { QueryClient } from '@tanstack/react-query'
import { createRootRouteWithContext, Link, Outlet } from '@tanstack/react-router'
import { TanStackRouterDevtools } from '@tanstack/router-devtools'
import { useSyncExternalStore } from 'react'
import { isSessionRejected, subscribeSessionRejected } from '@/api/client'
import { useHealth } from '@/api/queries'
import { LogoutButton } from '@/components/LogoutButton'
import { SessionRejectedNotice } from '@/components/SessionRejectedNotice'
import { SproutMark } from '@/components/SproutMark'
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

  // Set when the server reports a session signed by a different secret
  const sessionRejected = useSyncExternalStore(subscribeSessionRejected, isSessionRejected)

  // Determine display status
  // Priority: Session rejected > Server down > WS connected > WS connecting > Polling
  let statusLabel: string
  let statusColor: string
  let statusTooltip: string

  if (sessionRejected) {
    statusLabel = 'Unauthorized'
    statusColor = 'bg-red-500'
    statusTooltip = 'Session was signed by a different server process.'
  } else if (isServerDown) {
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
          {/* Below md: logo + status/logout on the first row, nav below */}
          <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2 py-2 md:flex-nowrap md:h-14 md:py-0">
            {/* Logo */}
            <Link to="/" className="flex items-center gap-2 shrink-0">
              <SproutMark size={22} className="shrink-0" />
              <span className="flex items-baseline gap-2">
                <span className="text-lg font-bold bg-gradient-to-r from-green-400 to-emerald-500 bg-clip-text text-transparent">
                  stemtrace
                </span>
                {health?.version && (
                  <span className="hidden sm:inline text-xs text-slate-500">v{health.version}</span>
                )}
              </span>
            </Link>

            {/* Navigation */}
            <nav className="order-last w-full flex items-center gap-5 md:order-none md:w-auto md:gap-6">
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
                to="/workers"
                className="text-sm text-slate-400 hover:text-slate-100 transition-colors [&.active]:text-slate-100"
              >
                Workers
              </Link>
              <Link
                to="/registry"
                className="text-sm text-slate-400 hover:text-slate-100 transition-colors [&.active]:text-slate-100"
              >
                Registry
              </Link>
            </nav>

            <div className="flex items-center gap-4 shrink-0">
              {/* Status indicator with tooltip */}
              <Tooltip content={statusTooltip} placement="bottom-end">
                <div className="flex items-center gap-2 cursor-help">
                  <span className={`w-2 h-2 rounded-full ${statusColor}`} />
                  <span className="text-xs text-slate-500">{statusLabel}</span>
                </div>
              </Tooltip>
              <LogoutButton />
            </div>
          </div>
        </div>
      </header>

      {/* Main content */}
      <main className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
        {sessionRejected ? <SessionRejectedNotice /> : <Outlet />}
      </main>

      {/* Dev tools */}
      {import.meta.env.DEV && <TanStackRouterDevtools position="bottom-right" />}
    </div>
  )
}
