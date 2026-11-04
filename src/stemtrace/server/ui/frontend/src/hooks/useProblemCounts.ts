import { useQuery } from '@tanstack/react-query'
import { registrySnapshot, workersSnapshot } from '@/api/queries'
import { offlineHostCount } from '@/components/workers/workers'

// The Workers and Registry badges never trigger Celery inspect (refresh=false): the request that
// asks waits for the inspect, which D7a bounds (about 5 s with the broker down, up to 7 s when it
// joins one already running). They show what the server already knows; the Workers and Registry
// pages refresh it. The anomaly badge asks, so its `unregistered` check does not lapse on an idle
// fleet; its inspect is bounded the same way.
const BADGE_POLL_MS = 30_000

/** Counts behind the sidebar's problem badges; null while unknown (no badge is shown). */
export function useProblemCounts(): {
  offlineWorkers: number | null
  notRegistered: number | null
} {
  const workers = useQuery({
    ...workersSnapshot,
    refetchInterval: BADGE_POLL_MS,
    retry: false,
  })
  const registry = useQuery({
    ...registrySnapshot,
    refetchInterval: BADGE_POLL_MS,
    retry: false,
  })
  return {
    offlineWorkers: workers.data ? offlineHostCount(workers.data.workers) : null,
    notRegistered: registry.data
      ? registry.data.tasks.filter((t) => t.status === 'not_registered').length
      : null,
  }
}
