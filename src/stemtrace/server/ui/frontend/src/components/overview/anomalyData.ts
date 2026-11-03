import {
  type Anomaly,
  type AnomalyKind,
  anomalyTotal,
  useAnomalyBadge,
  useFleetAnomaly,
} from '@/api/anomalies'

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
 * The anomalies for the Overview. It reads what the server already knows (`refresh=false`, the
 * sidebar badge's own query), so a dead broker cannot block the page. `failed` is true when the
 * request fails (an older server answers 404): the page then says nothing about anomalies.
 */
export function useOverviewAnomalies(): { data: OverviewAnomalies | undefined; failed: boolean } {
  const list = useAnomalyBadge()
  const data = list.data
  const inList = data?.anomalies.find((a) => a.kind === 'no_workers_online')
  // The list is capped and sorts the fleet item last, so a full one may not hold it
  const fleetQuery = useFleetAnomaly(Boolean(data?.counts.no_workers_online) && !inList, {
    refresh: false,
  })
  if (!data) return { data: undefined, failed: list.isError }
  return {
    data: {
      items: data.anomalies.filter((a) => a.kind !== 'no_workers_online'),
      fleet: inList ?? fleetQuery.data?.anomalies[0],
      counts: data.counts,
      total: anomalyTotal(data.counts),
    },
    failed: false,
  }
}
