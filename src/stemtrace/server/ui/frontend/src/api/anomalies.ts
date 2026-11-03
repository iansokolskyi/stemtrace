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

export async function fetchAnomalies(): Promise<AnomalyListResponse> {
  return apiFetch(`${API_BASE}/anomalies?limit=${ANOMALIES_LIMIT}`, 'Failed to fetch anomalies')
}

// Anomalies are judged per request against the clock, so they are polled, never pushed.
const ANOMALIES_POLL_MS = 30_000

/**
 * The flagged items, oldest first, with a count per kind. The Anomalies page and the sidebar
 * badge share this one query. A 404 (a server without the endpoint) stops the polling; nothing
 * is retried.
 */
export function useAnomalies() {
  return useQuery<AnomalyListResponse>({
    queryKey: ['anomalies'],
    queryFn: fetchAnomalies,
    refetchInterval: (query) => {
      const error = query.state.error
      return error instanceof HttpError && error.status === 404 ? false : ANOMALIES_POLL_MS
    },
    retry: false,
  })
}

/** How many items there are: `counts` counts items (a name with 40 waiting tasks is one). */
export function anomalyTotal(counts: Record<AnomalyKind, number>): number {
  return Object.values(counts).reduce((sum, n) => sum + n, 0)
}
