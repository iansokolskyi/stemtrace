import { useCallback, useSyncExternalStore } from 'react'

export type Theme = 'light' | 'dark'

const STORAGE_KEY = 'stemtrace-theme'
const DARK_QUERY = '(prefers-color-scheme: dark)'
const listeners = new Set<() => void>()

function storedTheme(): Theme | null {
  try {
    const value = localStorage.getItem(STORAGE_KEY)
    return value === 'light' || value === 'dark' ? value : null
  } catch {
    return null // storage blocked: follow the OS
  }
}

function effectiveTheme(): Theme {
  return storedTheme() ?? (window.matchMedia(DARK_QUERY).matches ? 'dark' : 'light')
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener)
  const media = window.matchMedia(DARK_QUERY)
  media.addEventListener('change', listener)
  window.addEventListener('storage', listener)
  return () => {
    listeners.delete(listener)
    media.removeEventListener('change', listener)
    window.removeEventListener('storage', listener)
  }
}

/**
 * The effective theme: the stored choice, else the OS preference. `setTheme` stores the choice
 * and sets `data-theme` on <html>, which the tokens key on (index.html applies it before paint).
 */
export function useTheme(): { theme: Theme; setTheme: (next: Theme) => void } {
  const theme = useSyncExternalStore(subscribe, effectiveTheme)
  const setTheme = useCallback((next: Theme) => {
    try {
      localStorage.setItem(STORAGE_KEY, next)
    } catch {
      // storage blocked: the choice lasts until reload
    }
    document.documentElement.dataset.theme = next
    for (const listener of listeners) listener()
  }, [])
  return { theme, setTheme }
}
