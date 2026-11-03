import { useNavigate, useSearch } from '@tanstack/react-router'
import { createContext, type ReactElement, type ReactNode, useContext, useMemo } from 'react'
import { formatPanel, type PanelEntry, parsePanel } from './panelState'

/** The triage panel's stack and the moves on it. Every move is a history entry, so Back undoes it. */
export interface PanelApi {
  stack: PanelEntry[]
  /** The opener's key (`data-tk`): what the stack's first entry was opened from. */
  activeKey: string | undefined
  /** Replace the stack with one entry (an opener on the page). */
  open: (entry: PanelEntry) => void
  /** Add an entry on top (a row inside the panel). */
  push: (entry: PanelEntry) => void
  /** Remove the top entry; the last one closes the panel. */
  pop: () => void
  /** Keep the first `length` entries (a crumb). */
  popTo: (length: number) => void
  close: () => void
}

/** The key an opener carries in `data-tk`, so the panel can mark it and return focus to it. */
export const openerKey = (entry: PanelEntry): string => `${entry.kind}:${entry.id}`

/**
 * The props that make an element open `entry` in the panel: its key, a `data-on` mark while it
 * is the stack's first entry (the highlighted opener), and the click.
 */
export function openerProps(
  api: PanelApi,
  entry: PanelEntry,
): { 'data-tk': string; 'data-on': string | undefined; onClick: () => void } {
  const key = openerKey(entry)
  return {
    'data-tk': key,
    'data-on': api.activeKey === key ? '' : undefined,
    onClick: () => api.open(entry),
  }
}

const PanelContext = createContext<PanelApi | null>(null)

/** Provides the panel's state, read from and written to the Overview's `panel` search param. */
export function PanelProvider({ children }: { children: ReactNode }): ReactElement {
  const navigate = useNavigate()
  const panel = useSearch({ from: '/', select: (search) => search.panel })
  const stack = useMemo(() => parsePanel(panel), [panel])

  const api = useMemo<PanelApi>(() => {
    const go = (next: PanelEntry[]) =>
      navigate({ to: '/', search: (prev) => ({ ...prev, panel: formatPanel(next) }) })
    return {
      stack,
      activeKey: stack[0] && openerKey(stack[0]),
      open: (entry) => go([entry]),
      push: (entry) => {
        const top = stack[stack.length - 1]
        if (top && openerKey(top) === openerKey(entry)) return
        go([...stack, entry])
      },
      pop: () => go(stack.slice(0, -1)),
      popTo: (length) => go(stack.slice(0, length)),
      close: () => go([]),
    }
  }, [navigate, stack])

  return <PanelContext.Provider value={api}>{children}</PanelContext.Provider>
}

/** The panel's state and moves; only inside `PanelProvider`. */
export function usePanel(): PanelApi {
  const api = useContext(PanelContext)
  if (!api) throw new Error('usePanel needs a PanelProvider')
  return api
}
