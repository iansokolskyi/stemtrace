import { useCallback, useState } from 'react'
import { DEFAULT_DENSITY, type Density } from '@/components/executions/density'

const STORAGE_KEY = 'stemtrace-density'

function isDensity(value: unknown): value is Density {
  return value === 'compact' || value === 'comfortable'
}

function readStored(): Density {
  try {
    const stored = window.localStorage.getItem(STORAGE_KEY)
    return isDensity(stored) ? stored : DEFAULT_DENSITY
  } catch {
    // Private windows and blocked site data: the choice just doesn't outlive the page
    return DEFAULT_DENSITY
  }
}

/**
 * The Executions row density, a per-viewer preference kept in localStorage (never the URL: a
 * shared link should not change how the recipient's table is packed). Works without storage.
 */
export function useDensity(): [Density, (density: Density) => void] {
  const [density, setDensityState] = useState<Density>(readStored)
  const setDensity = useCallback((next: Density) => {
    setDensityState(next)
    try {
      window.localStorage.setItem(STORAGE_KEY, next)
    } catch {
      // Not stored; the choice still applies until the page closes
    }
  }, [])
  return [density, setDensity]
}
