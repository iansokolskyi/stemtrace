import { createFileRoute, redirect } from '@tanstack/react-router'
import { executionsSearch } from './-search'

// Until the Overview page lands, `/` opens Executions. Old task-list links (`/?state=…`)
// keep their filters.
export const Route = createFileRoute('/')({
  validateSearch: executionsSearch,
  beforeLoad: ({ search }) => {
    throw redirect({ to: '/executions', search, replace: true })
  },
})
