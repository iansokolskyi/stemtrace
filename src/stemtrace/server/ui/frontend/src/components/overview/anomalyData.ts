import {
  type Anomaly,
  type AnomalyKind,
  anomalyTotal,
  useAnomalyBadge,
  useFleetAnomaly,
} from '@/api/anomalies'
import { useViewState, type ViewState } from '@/hooks/useViewState'

/** What the Overview shows of the anomalies: the listed items, the fleet item and the counts. */
export interface OverviewAnomalies {
  /** Every item but the fleet one, oldest first (the API's order). */
  items: Anomaly[]
  /** The `no_workers_online` item, wherever the capped list put it. */
  fleet: Anomaly | undefined
  counts: Record<AnomalyKind, number>
  /** Items in all, the fleet one included. */
  total: number
}

/**
 * The anomalies for the Overview. It shares the sidebar badge's query, which asks the workers:
 * the request waits for the inspect (about 5 s at most with the broker down, up to 7 s when it
 * joins one already running; inside a backoff window it answers at once). `failed` is true when the
 * request fails (an older server answers 404): the page then says nothing about anomalies.
 */
export function useOverviewAnomalies(): {
  data: OverviewAnomalies | undefined
  failed: boolean
  /** The list or the fleet item failed to refresh while earlier data is held. */
  stale: boolean
  /** The queries behind it, for the page's stale line. */
  views: ViewState[]
} {
  const list = useAnomalyBadge()
  const data = list.data
  const inList = data?.anomalies.find((a) => a.kind === 'no_workers_online')
  // The list is capped and sorted by `since`, so a full one may not hold the fleet item
  const fleetAsked = Boolean(data?.counts.no_workers_online) && !inList
  const fleetQuery = useFleetAnomaly(fleetAsked)
  const listView = useViewState(list, 'anomalies/badge')
  const fleetView = useViewState(fleetQuery, 'anomalies/fleet')
  // A fleet query that is no longer asked for keeps its last error but has nothing to refresh,
  // so it must not hold the stale line
  const views = fleetAsked ? [listView, fleetView] : [listView]
  const stale = views.some((v) => v.stale)
  if (!data) return { data: undefined, failed: listView.phase === 'error', stale, views }
  return {
    data: {
      items: data.anomalies.filter((a) => a.kind !== 'no_workers_online'),
      fleet: inList ?? fleetQuery.data?.anomalies[0],
      counts: data.counts,
      total: anomalyTotal(data.counts),
    },
    failed: false,
    stale,
    views,
  }
}
