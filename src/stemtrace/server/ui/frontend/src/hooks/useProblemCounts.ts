import { useQuery } from '@tanstack/react-query'
import { fetchTaskRegistry, fetchWorkers } from '@/api/client'

// Badges never trigger Celery inspect (refresh=false): an inspect blocks the request for a
// second or more, and much longer while the broker is down. They show what the server already
// knows; the Workers and Registry pages refresh it.
const BADGE_POLL_MS = 30_000

/** Counts behind the sidebar's problem badges; null while unknown (no badge is shown). */
export function useProblemCounts(): {
  offlineWorkers: number | null
  notRegistered: number | null
} {
  const workers = useQuery({
    queryKey: ['badges', 'workers'],
    queryFn: () => fetchWorkers(undefined, { refresh: false }),
    refetchInterval: BADGE_POLL_MS,
    retry: false,
  })
  const registry = useQuery({
    queryKey: ['badges', 'registry'],
    queryFn: () => fetchTaskRegistry(undefined, undefined, { refresh: false }),
    refetchInterval: BADGE_POLL_MS,
    retry: false,
  })
  return {
    offlineWorkers: workers.data
      ? workers.data.workers.filter((w) => w.status === 'offline').length
      : null,
    notRegistered: registry.data
      ? registry.data.tasks.filter((t) => t.status === 'not_registered').length
      : null,
  }
}
