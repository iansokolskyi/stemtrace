import { createFileRoute, redirect } from '@tanstack/react-router'
import { flowsSearch } from './-search'

// Old URL: the graphs list is now Flows.
export const Route = createFileRoute('/graphs')({
  validateSearch: flowsSearch,
  beforeLoad: ({ search }) => {
    throw redirect({ to: '/flows', search, replace: true })
  },
})
