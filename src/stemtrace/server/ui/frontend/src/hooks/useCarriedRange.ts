import { useSyncExternalStore } from 'react'
import type { Range } from '@/routes/-search'

// The range the Executions page last showed (undefined: the default). In-app links to
// Executions carry it, so the range survives leaving the page, as in the prototype.
let carried: Range | undefined
const listeners = new Set<() => void>()

/** Remembers the range for the links to Executions. */
export function carryRange(range: Range | undefined): void {
  if (range === carried) return
  carried = range
  for (const listener of listeners) listener()
}

/** The range a link to Executions carries: `{ range }`, with `range` absent for the default. */
export function useCarriedRange(): { range?: Range } {
  const range = useSyncExternalStore(
    (listener) => {
      listeners.add(listener)
      return () => {
        listeners.delete(listener)
      }
    },
    () => carried,
  )
  return { range }
}
