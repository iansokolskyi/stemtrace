import { type ReactElement, useEffect, useLayoutEffect, useRef, useState } from 'react'
import type { ExecutionFilters } from '@/api/client'
import { parseDateParam } from '@/utils/format'
import { Icon } from '../Icon'
import { FIELD_LABEL, type FilterField, FilterPopover, stateWord } from './FilterPopover'
import type { FilterSuggestions } from './suggestions'

type ChipKey = Exclude<keyof ExecutionFilters, 'from_date' | 'to_date'> | 'time'

interface Chip {
  key: ChipKey
  /** The filter's name on the chip; "Task" is the exact task name. */
  label: string
  value: string
  field: FilterField
  exact?: boolean
}

const POPOVER_WIDTH = 288
const SHORT_DATE: Intl.DateTimeFormatOptions = { month: 'short', day: 'numeric', year: 'numeric' }

const day = (value: string | undefined): string | undefined => {
  const date = parseDateParam(value)
  return date?.toLocaleDateString('en-US', SHORT_DATE)
}

const FIELD_CHIPS = [
  { key: 'exception_type', label: 'Exception' },
  { key: 'hostname', label: 'Worker' },
  { key: 'queue', label: 'Queue' },
  { key: 'task_group', label: 'Group' },
  { key: 'tag', label: 'Tag' },
] as const

/** What the time chip says: both dates, one date, or "From"/"Until" for an open end. */
function timeValue(filters: ExecutionFilters): string | undefined {
  const from = day(filters.from_date)
  const to = day(filters.to_date)
  if (from && to) return from === to ? from : `${from} - ${to}`
  if (from) return `From ${from}`
  return to ? `Until ${to}` : undefined
}

/** The chips for the filters that are set, in a fixed order. */
function chipsOf(filters: ExecutionFilters): Chip[] {
  const chips: Chip[] = []
  if (filters.state) {
    const value = filters.state.split(',').map(stateWord).join(', ')
    chips.push({ key: 'state', label: 'State', value, field: 'state' })
  }
  if (filters.name) {
    chips.push({ key: 'name', label: 'Name', value: filters.name, field: 'name' })
  }
  if (filters.name_exact) {
    const value = filters.name_exact
    chips.push({ key: 'name_exact', label: 'Task', value, field: 'name', exact: true })
  }
  for (const { key, label } of FIELD_CHIPS) {
    const value = filters[key]
    if (value) chips.push({ key, label, value, field: key })
  }
  const time = timeValue(filters)
  if (time) chips.push({ key: 'time', label: 'Time', value: time, field: 'time' })
  return chips
}

/** The filters as a string that doesn't depend on key order or on unset keys. */
const canonical = (filters: ExecutionFilters): string =>
  JSON.stringify(
    Object.entries(filters)
      .filter(([, value]) => value !== undefined)
      .sort(([a], [b]) => a.localeCompare(b)),
  )

/** What removing a chip clears. */
function without(filters: ExecutionFilters, key: ChipKey): ExecutionFilters {
  if (key === 'time') return { ...filters, from_date: undefined, to_date: undefined }
  return { ...filters, [key]: undefined }
}

interface FilterChipsProps {
  filters: ExecutionFilters
  /** Sets the filters. `replace` is true for later steps of one edit, so Back undoes the edit. */
  onChange: (next: ExecutionFilters, options: { replace: boolean }) => void
  suggestions: FilterSuggestions
}

interface Editing {
  /** What opened the popover: a chip's key, or 'add'. */
  by: string
  /** null: the list of fields. */
  field: FilterField | null
  exact: boolean
}

/**
 * The active filters as chips, an "Add filter" button and a popover to set or edit one. State
 * is in the URL, not here: the chips are drawn from `filters`, so Back restores them.
 */
export function FilterChips({ filters, onChange, suggestions }: FilterChipsProps): ReactElement {
  const chips = chipsOf(filters)
  const [editing, setEditing] = useState<Editing | null>(null)
  const barRef = useRef<HTMLDivElement>(null)
  const popoverRef = useRef<HTMLDialogElement>(null)
  const addRef = useRef<HTMLButtonElement>(null)
  const openerRef = useRef<HTMLElement | null>(null)
  // One edit is one history entry: its first change is pushed, the rest replace it
  const pushed = useRef(false)
  const [left, setLeft] = useState(0)
  // The filters the open popover last produced. If they change any other way (the browser's Back
  // or Forward), the popover closes: its edit is over, and the next one must push a new entry.
  const filtersKey = canonical(filters)
  const expected = useRef(filtersKey)

  const openFrom = (opener: HTMLElement, next: Editing) => {
    if (editing?.by === next.by) return close()
    openerRef.current = opener
    pushed.current = false
    expected.current = filtersKey
    setEditing(next)
  }

  /** Hands what a field in the popover is still holding (a typed date) to its own blur handler. */
  const settleFocused = () => {
    const focused = document.activeElement
    if (focused instanceof HTMLElement && popoverRef.current?.contains(focused)) focused.blur()
  }

  const close = () => {
    settleFocused()
    setEditing(null)
    const opener = openerRef.current
    // The opener can be gone (its chip was cleared): fall back to the add button
    const target = opener?.isConnected ? opener : addRef.current
    requestAnimationFrame(() => target?.focus())
  }

  const commit = (next: ExecutionFilters, shouldClose: boolean) => {
    expected.current = canonical(next)
    onChange(next, { replace: pushed.current })
    pushed.current = true
    if (shouldClose) close()
  }

  // biome-ignore lint/correctness/useExhaustiveDependencies: filtersKey is the trigger
  useEffect(() => {
    if (editing && filtersKey !== expected.current) setEditing(null)
  }, [filtersKey])

  // Keep the popover under its opener when the window resizes, the chips wrap differently, or a
  // chip is added or removed ahead of it (which moves the opener without resizing the bar)
  // biome-ignore lint/correctness/useExhaustiveDependencies: filtersKey is a trigger
  useLayoutEffect(() => {
    if (!editing) return
    const measure = () => {
      const bar = barRef.current
      const opener = openerRef.current
      if (!bar || !opener?.isConnected) return
      const room = Math.max(0, bar.clientWidth - Math.min(POPOVER_WIDTH, bar.clientWidth))
      setLeft(Math.min(opener.offsetLeft, room))
    }
    measure()
    const observer = new ResizeObserver(measure)
    if (barRef.current) observer.observe(barRef.current)
    window.addEventListener('resize', measure)
    return () => {
      observer.disconnect()
      window.removeEventListener('resize', measure)
    }
  }, [editing, filtersKey])

  // Focus the first control when the popover opens or switches field
  const shown = editing === null ? null : (editing.field ?? 'list')
  useLayoutEffect(() => {
    if (shown === null) return
    popoverRef.current?.querySelector<HTMLElement>('input, button')?.focus()
  }, [shown])

  // Escape and a click outside close the popover
  // biome-ignore lint/correctness/useExhaustiveDependencies: close only reads refs
  useEffect(() => {
    if (!editing) return
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') close()
    }
    const onDown = (event: MouseEvent) => {
      const target = event.target as Node
      if (popoverRef.current?.contains(target) || openerRef.current?.contains(target)) return
      // Closing unmounts the field before it would lose focus: let it commit first
      settleFocused()
      setEditing(null)
    }
    document.addEventListener('keydown', onKey)
    document.addEventListener('mousedown', onDown)
    return () => {
      document.removeEventListener('keydown', onKey)
      document.removeEventListener('mousedown', onDown)
    }
  }, [editing])

  return (
    <div ref={barRef} className="relative flex flex-wrap items-center gap-2">
      {chips.map((chip) => (
        <span
          key={chip.key}
          data-chip={chip.key}
          className="inline-flex h-[26px] max-w-full items-center rounded-[3px] bg-raised text-xs"
        >
          <button
            type="button"
            aria-haspopup="dialog"
            aria-expanded={editing?.by === chip.key}
            onClick={(event) =>
              openFrom(event.currentTarget, {
                by: chip.key,
                field: chip.field,
                exact: !!chip.exact,
              })
            }
            className="flex h-full min-w-0 items-center gap-1.5 rounded-[3px] pr-1 pl-2.5 hover:bg-hover"
          >
            <span className="flex-none text-muted">{chip.label}:</span>
            <b className="min-w-0 truncate font-bold" title={chip.value}>
              {chip.value}
            </b>
          </button>
          <button
            type="button"
            aria-label={`Remove ${chip.label.toLowerCase()} filter`}
            onClick={() => onChange(without(filters, chip.key), { replace: false })}
            className="mr-0.5 flex size-[18px] flex-none items-center justify-center rounded-[2px] text-muted hover:bg-hover hover:text-ink"
          >
            ×
          </button>
        </span>
      ))}
      <button
        ref={addRef}
        type="button"
        aria-haspopup="dialog"
        aria-expanded={editing?.by === 'add'}
        onClick={(event) => openFrom(event.currentTarget, { by: 'add', field: null, exact: false })}
        className="flex h-[26px] items-center gap-1.5 rounded-[3px] bg-raised px-2.5 text-xs font-semibold text-ink hover:bg-press"
      >
        <Icon name="plus" size={13} />
        Add filter
      </button>
      {chips.length > 0 && (
        <button
          type="button"
          onClick={() => onChange({}, { replace: false })}
          className="h-[26px] rounded-[3px] px-2 text-xs text-muted hover:text-ink"
        >
          Clear
        </button>
      )}
      {editing && (
        <dialog
          ref={popoverRef}
          open
          aria-label={editing.field ? `${FIELD_LABEL[editing.field]} filter` : 'Add filter'}
          className="absolute top-full z-30 m-0 mt-1.5 rounded-lg bg-surface p-1.5 text-ink shadow-pop"
          style={{ left, width: `min(${POPOVER_WIDTH}px, 100%)` }}
        >
          <FilterPopover
            field={editing.field}
            exact={editing.exact}
            filters={filters}
            suggestions={suggestions}
            onCommit={commit}
            onPickField={(field) => setEditing({ by: editing.by, field, exact: false })}
          />
        </dialog>
      )}
    </div>
  )
}
