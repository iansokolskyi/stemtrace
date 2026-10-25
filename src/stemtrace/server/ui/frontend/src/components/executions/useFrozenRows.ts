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

/** When a row was last updated, as a number the list is sorted by; unknown sorts last. */
const updatedAt = (row: ExecutionRow): number =>
  row.last_updated ? Date.parse(row.last_updated) : Number.NEGATIVE_INFINITY

/**
 * Keeps the visible order still while the reader is looking at it.
 *
 * The server lists tasks by `last_updated`, newest first, so any event moves its task to the top
 * and a new task pushes the bottom row out of a page. Which rows are "new" therefore can't depend
 * on where one particular held row sits in the live list. A task the reader hasn't been shown is
 * new when it was updated after the oldest row they were shown (it sorts above something on
 * screen); one updated before that is an older page loading and is appended.
 *
 * While `frozen`, the rows already shown keep their order and position but take the live data (a
 * state, a runtime or a worker changing shows in place). A shown row that left the live list (it
 * no longer matches the filters, or the first page no longer reaches it) is dropped, never kept
 * with stale data. New tasks are counted, not drawn. When `frozen` ends the live order applies.
 */
export function useFrozenRows(live: ExecutionRow[], frozen: boolean): FrozenRows {
  // The order held while frozen: the live list as of the last unfrozen render, or the last apply
  const [held, setHeld] = useState<ExecutionRow[]>(live)
  // Adjusting state during render (not in an effect) so no frame draws the stale order
  if (!frozen && held !== live) setHeld(live)
  const apply = useCallback(() => setHeld(live), [live])

  if (!frozen) return { shown: live, newCount: 0, apply }

  const latest = new Map(live.map((row) => [row.task_id, row]))
  const shown: ExecutionRow[] = []
  for (const row of held) {
    const current = latest.get(row.task_id)
    if (current) shown.push(current)
  }
  const heldIds = new Set(held.map((row) => row.task_id))
  // The oldest update the reader was shown; with nothing held, every row is the first load
  const floor = held.length ? Math.min(...held.map(updatedAt)) : Number.NEGATIVE_INFINITY

  let newCount = 0
  for (const row of live) {
    if (heldIds.has(row.task_id)) continue
    if (updatedAt(row) > floor) newCount++
    else shown.push(row)
  }
  return { shown, newCount, apply }
}
