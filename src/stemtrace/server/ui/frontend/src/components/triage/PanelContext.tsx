import { useNavigate, useSearch } from '@tanstack/react-router'
import { createContext, type ReactElement, type ReactNode, useContext, useMemo } from 'react'
import { formatPanel, type PanelEntry, parsePanel } from './panelState'

/** The triage panel's stack and the moves on it. Every move but `refine` is a history entry, so Back undoes it. */
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
  /** Change the view (chips, "Show more") of the entry at `index`, in place in the history. `update` gets the view as the URL has it now, so two quick changes both land. */
  refine: (index: number, update: (now: EntryView) => EntryView) => void
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

/** The stack position of the entry the component below is rendered for. */
export const EntryIndex = createContext(0)

/** The view an entry is in: what it keeps in the URL beside its kind and id. */
export type EntryView = Pick<PanelEntry, 'states' | 'more'>

/**
 * The view of the entry the component is rendered for, and a setter for it. The view is in the
 * `panel` URL value, so a reload, a shared link, Esc and Back restore it. A change replaces the
 * history entry: the chips and "Show more" refine a view, they are not steps to go back through.
 */
export function useEntryView(): [EntryView, (update: (now: EntryView) => EntryView) => void] {
  const panel = usePanel()
  const index = useContext(EntryIndex)
  const entry = panel.stack[index]
  return [{ states: entry?.states, more: entry?.more }, (update) => panel.refine(index, update)]
}

const PanelContext = createContext<PanelApi | null>(null)

/** Provides the panel's state, read from and written to the Overview's `panel` search param. */
export function PanelProvider({ children }: { children: ReactNode }): ReactElement {
  const navigate = useNavigate()
  const panel = useSearch({ from: '/', select: (search) => search.panel })
  const stack = useMemo(() => parsePanel(panel), [panel])

  const api = useMemo<PanelApi>(() => {
    const go = (next: PanelEntry[], replace = false) =>
      navigate({ to: '/', search: (prev) => ({ ...prev, panel: formatPanel(next) }), replace })
    return {
      stack,
      activeKey: stack[0] && openerKey(stack[0]),
      open: (entry) => {
        // Opening the entry at the bottom keeps its view: nothing at depth 1, else the entries
        // above it close (one history entry)
        if (stack[0] && openerKey(stack[0]) === openerKey(entry)) {
          if (stack.length > 1) go(stack.slice(0, 1))
          return
        }
        go([entry])
      },
      push: (entry) => {
        const top = stack[stack.length - 1]
        if (top && openerKey(top) === openerKey(entry)) return
        go([...stack, entry])
      },
      pop: () => go(stack.slice(0, -1)),
      popTo: (length) => go(stack.slice(0, length)),
      close: () => go([]),
      refine: (index, update) =>
        navigate({
          to: '/',
          // The latest URL, not the render's stack: a second change before a render sees the first
          search: (prev) => {
            const now = parsePanel(prev.panel)
            if (!now[index]) return prev
            const { states: _s, more: _m, ...rest } = now[index]
            const next = {
              ...rest,
              ...update({ states: now[index].states, more: now[index].more }),
            }
            return { ...prev, panel: formatPanel(now.map((e, i) => (i === index ? next : e))) }
          },
          replace: true,
        }),
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
