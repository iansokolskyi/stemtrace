import { createFileRoute, redirect } from '@tanstack/react-router'

// Old URL: a graph is now a flow.
export const Route = createFileRoute('/graph/$rootId')({
  beforeLoad: ({ params }) => {
    throw redirect({ to: '/flows/$rootId', params, replace: true })
  },
})
