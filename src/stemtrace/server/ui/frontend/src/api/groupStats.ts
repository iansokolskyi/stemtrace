import type { GroupTimeseriesResponse } from './client'

/** Failures over runs per group, from the per-bucket counts by state; a group without runs is 0. */
export function groupFailureRates(response: GroupTimeseriesResponse): Map<string, number> {
  const rates = new Map<string, number>()
  for (const [group, byState] of Object.entries(response.by_task_group ?? {})) {
    let runs = 0
    let failures = 0
    for (const [state, buckets] of Object.entries(byState)) {
      const count = buckets.reduce((sum, n) => sum + n, 0)
      runs += count
      if (state === 'FAILURE') failures += count
    }
    rates.set(group, runs ? failures / runs : 0)
  }
  return rates
}
