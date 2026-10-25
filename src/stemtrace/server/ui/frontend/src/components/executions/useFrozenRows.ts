import { useCallback, useState } from 'react'
import type { ExecutionRow } from '@/api/client'

interface FrozenRows {
  /** The rows to draw. */
  shown: ExecutionRow[]
  /** Tasks the live list would insert above the rows on screen, held back while frozen. */
  newCount: number
  /** Adopts the live order now (the "N new" button). */
  apply: () => void
}

/**
 * Keeps the visible order still while the reader is looking at it.
 *
 * While `frozen`, the rows already shown keep their order and position but take the live data
 * (a state, a runtime or a worker changing shows in place); a task the live list would insert
 * above the last shown row is only counted, not drawn. A task that sorts after the last shown
 * row is an older page loading, so it is appended. When `frozen` ends the live order applies.
 */
export function useFrozenRows(live: ExecutionRow[], frozen: boolean): FrozenRows {
  // The order held while frozen: the live list as of the last unfrozen render, or the last apply
  const [held, setHeld] = useState<ExecutionRow[]>(live)
  // Adjusting state during render (not in an effect) so no frame draws the stale order
  if (!frozen && held !== live) setHeld(live)
  const apply = useCallback(() => setHeld(live), [live])

  if (!frozen) return { shown: live, newCount: 0, apply }

  const latest = new Map(live.map((row) => [row.task_id, row]))
  const shown = held.map((row) => latest.get(row.task_id) ?? row)
  const heldIds = new Set(held.map((row) => row.task_id))
  const lastHeld = held.length ? held[held.length - 1].task_id : null
  const lastIndex = lastHeld === null ? -1 : live.findIndex((row) => row.task_id === lastHeld)

  let newCount = 0
  live.forEach((row, index) => {
    if (heldIds.has(row.task_id)) return
    if (lastIndex >= 0 && index < lastIndex) newCount++
    else shown.push(row)
  })
  return { shown, newCount, apply }
}
