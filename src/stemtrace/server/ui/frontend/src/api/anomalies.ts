/**
 * Anomaly types and the `GET /api/anomalies` client (the one owner of both).
 *
 * `AnomalyKind` and `Anomaly` mirror `AnomalyResponse` in `server/api/schemas.py`; the
 * `anomaly` field on task and graph nodes and `anomaly_detail` on a task page use them too.
 */

import { useQuery } from '@tanstack/react-query'
import { API_BASE, apiFetch, HttpError } from './client'

export type AnomalyKind = 'stuck' | 'interrupted' | 'unregistered' | 'no_workers_online'

/** What showed a worker stopped while it ran a task (interrupted only). */
export type AnomalyEvidence =
  | 'worker_shutdown'
  | 'worker_terminating'
  | 'worker_restarted'
  | 'cancelled_retry'

/** One flagged item: a task, a task name (unregistered), or the fleet (`no_workers_online`). */
export interface Anomaly {
  kind: AnomalyKind
  /** Null for an unregistered name and for `no_workers_online`. */
  task_id: string | null
  /** Null only for `no_workers_online`. */
  name: string | null
  /** Stuck: its start; interrupted: the worker's stop; else the oldest PENDING. UTC ISO 8601. */
  since: string
  hostname: string | null
  queue: string | null
  /** Unregistered and `no_workers_online`: how many tasks are PENDING. */
  task_count: number
  /** Stuck: time without progress, the threshold, the name's p95 (null without samples), its sample count. */
  elapsed_ms: number | null
  threshold_ms: number | null
  p95_ms: number | null
  runtime_samples: number | null
  evidence: AnomalyEvidence | null
  /** Unregistered: a registered name close to this one (a likely rename). */
  similar_name: string | null
}

export interface AnomalyListResponse {
  anomalies: Anomaly[]
  /** Every kind, 0 included; ignores the filters. */
  counts: Record<AnomalyKind, number>
  total: number
  limit: number
  offset: number
  evaluated_at: string
}

/** The most one page holds. */
const ANOMALIES_LIMIT = 500

/** How long a request may take before the page says so (the server may be asking a dead broker). */
export const ANOMALIES_TIMEOUT_MS = 10_000

/** The request ran past `ANOMALIES_TIMEOUT_MS`. */
export class AnomaliesTimeoutError extends Error {
  constructor() {
    super('Anomalies are taking long to load')
    this.name = 'AnomaliesTimeoutError'
  }
}

interface FetchAnomaliesOptions {
  /** False asks the server not to inspect the workers first (it answers from what it knows). */
  refresh?: boolean
  /** Only this kind. */
  kind?: AnomalyKind
}

export async function fetchAnomalies({
  refresh = true,
  kind,
}: FetchAnomaliesOptions = {}): Promise<AnomalyListResponse> {
  const params = new URLSearchParams({ limit: String(ANOMALIES_LIMIT) })
  if (!refresh) params.set('refresh', 'false')
  if (kind) params.set('kind', kind)
  try {
    return await apiFetch(
      `${API_BASE}/anomalies?${params}`,
      'Failed to fetch anomalies',
      AbortSignal.timeout(ANOMALIES_TIMEOUT_MS),
    )
  } catch (error) {
    if (error instanceof DOMException && error.name === 'TimeoutError') {
      throw new AnomaliesTimeoutError()
    }
    throw error
  }
}

// Anomalies are judged per request against the clock, so they are polled, never pushed.
const ANOMALIES_POLL_MS = 30_000

// A 404 (a server without the endpoint) stops the polling; nothing is retried.
function pollUnlessMissing(query: { state: { error: Error | null } }): number | false {
  const error = query.state.error
  return error instanceof HttpError && error.status === 404 ? false : ANOMALIES_POLL_MS
}

/** The Anomalies page's list: the server asks the workers first, so it is current. */
export function useAnomalies() {
  return useQuery<AnomalyListResponse>({
    queryKey: ['anomalies'],
    queryFn: () => fetchAnomalies(),
    refetchInterval: pollUnlessMissing,
    retry: false,
  })
}

/**
 * What the server already knows, for the sidebar badge on every page (`refresh=false`: asking
 * the workers blocks while the broker is down).
 */
export function useAnomalyBadge() {
  return useQuery<AnomalyListResponse>({
    queryKey: ['anomalies', 'badge'],
    queryFn: () => fetchAnomalies({ refresh: false }),
    refetchInterval: pollUnlessMissing,
    retry: false,
  })
}

/** The fleet item alone, for when the capped list does not hold it (it sorts last). */
export function useFleetAnomaly(enabled: boolean) {
  return useQuery<AnomalyListResponse>({
    queryKey: ['anomalies', 'fleet'],
    queryFn: () => fetchAnomalies({ kind: 'no_workers_online' }),
    enabled,
    refetchInterval: pollUnlessMissing,
    retry: false,
  })
}

/** How many items there are: `counts` counts items (a name with 40 waiting tasks is one). */
export function anomalyTotal(counts: Record<AnomalyKind, number>): number {
  return Object.values(counts).reduce((sum, n) => sum + n, 0)
}
