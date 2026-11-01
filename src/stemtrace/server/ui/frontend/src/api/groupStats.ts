import type { GroupTimeseriesResponse } from './client'

/**
 * The states a failure rate divides by: terminal runs except IGNORED (the task chose not to
 * run). Mirrors `OUTCOME_STATES` in `server/stats.py`, which the overview's failure rate uses.
 */
const OUTCOME_STATES: ReadonlySet<string> = new Set(['SUCCESS', 'FAILURE', 'REVOKED', 'REJECTED'])

/**
 * Failures over outcomes per group, from the per-bucket counts by state. A group with no
 * outcome is left out (it has no rate), and so is everything when the server sent no groups.
 */
export function groupFailureRates(response: GroupTimeseriesResponse): Map<string, number> {
  const rates = new Map<string, number>()
  for (const [group, byState] of Object.entries(response.by_task_group ?? {})) {
    let outcomes = 0
    let failures = 0
    for (const [state, buckets] of Object.entries(byState)) {
      if (!OUTCOME_STATES.has(state)) continue
      const count = buckets.reduce((sum, n) => sum + n, 0)
      outcomes += count
      if (state === 'FAILURE') failures += count
    }
    if (outcomes > 0) rates.set(group, failures / outcomes)
  }
  return rates
}
