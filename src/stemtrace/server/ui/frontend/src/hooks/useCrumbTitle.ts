import { useEffect, useSyncExternalStore } from 'react'

// What a detail page calls itself in the breadcrumb, in place of the ID in its URL (the Flow
// page: the root task's name). One page is open at a time, so one value is enough.
let current: string | undefined
const listeners = new Set<() => void>()

function set(title: string | undefined): void {
  if (title === current) return
  current = title
  for (const listener of listeners) listener()
}

/** The title the open page set, if any. */
export function useCrumbTitle(): string | undefined {
  return useSyncExternalStore(
    (listener) => {
      listeners.add(listener)
      return () => {
        listeners.delete(listener)
      }
    },
    () => current,
  )
}

/** Sets the breadcrumb's last crumb while the calling page is open. */
export function useSetCrumbTitle(title: string | undefined): void {
  useEffect(() => {
    set(title)
    return () => set(undefined)
  }, [title])
}
