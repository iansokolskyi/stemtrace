import type { QueryClient } from '@tanstack/react-query'
import { createRootRouteWithContext, Outlet } from '@tanstack/react-router'
import { TanStackRouterDevtools } from '@tanstack/router-devtools'
import { useSyncExternalStore } from 'react'
import { isSessionRejected, subscribeSessionRejected } from '@/api/client'
import { AppFrame } from '@/components/frame/AppFrame'
import { SessionRejectedNotice } from '@/components/SessionRejectedNotice'

interface RouterContext {
  queryClient: QueryClient
}

export const Route = createRootRouteWithContext<RouterContext>()({
  component: RootLayout,
})

function RootLayout() {
  // Set when the server reports a session signed by a different secret
  const sessionRejected = useSyncExternalStore(subscribeSessionRejected, isSessionRejected)

  return (
    <>
      <AppFrame>{sessionRejected ? <SessionRejectedNotice /> : <Outlet />}</AppFrame>
      {import.meta.env.DEV && <TanStackRouterDevtools position="bottom-right" />}
    </>
  )
}
