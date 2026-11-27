import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createRouter, RouterProvider } from '@tanstack/react-router'
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { isNotFound, UnauthorizedError } from './api/client'
import { WebSocketProvider } from './hooks/WebSocketContext'
import { routeTree } from './routeTree.gen'
// Imported here, not from index.css: Tailwind inlines CSS @imports, which breaks the
// packages' relative ./files/*.woff2 URLs; Vite resolves them when it loads the CSS itself.
import '@fontsource-variable/inter/opsz.css'
import '@fontsource-variable/jetbrains-mono/wght.css'
import './index.css'

// Create query client
const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 5000,
      refetchOnWindowFocus: false,
      // A 401 (the client is already redirecting to login) and a 404 (the item is not stored)
      // would answer the same again
      retry: (failureCount, error) =>
        !(error instanceof UnauthorizedError) && !isNotFound(error) && failureCount < 3,
    },
  },
})

// Get base path from injected config (set by server)
const basepath = window.__STEMTRACE_BASE__ ?? ''

// Create router with dynamic basepath
const router = createRouter({
  routeTree,
  basepath,
  context: {
    queryClient,
  },
  defaultPreload: 'intent',
})

// Register router for type safety
declare module '@tanstack/react-router' {
  interface Register {
    router: typeof router
  }
}

// Render
createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <WebSocketProvider>
        <RouterProvider router={router} />
      </WebSocketProvider>
    </QueryClientProvider>
  </StrictMode>,
)
