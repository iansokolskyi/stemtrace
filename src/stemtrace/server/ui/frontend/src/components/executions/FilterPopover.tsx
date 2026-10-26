import { subDays } from 'date-fns'
import { type ReactElement, useEffect, useId, useRef, useState } from 'react'
import type { ExecutionFilters } from '@/api/client'
import { TASK_STATES } from '@/routes/-search'
import { dateSearchParam, formatDateParam } from '@/utils/format'
import type { FilterSuggestions } from './suggestions'

/** The filters a popover edits (the Name editor covers `name` and `name_exact`). */
export type FilterField =
  | 'state'
  | 'name'
  | 'exception_type'
  | 'hostname'
  | 'queue'
  | 'task_group'
  | 'tag'
  | 'time'

export const FIELD_LABEL: Record<FilterField, string> = {
  state: 'State',
  name: 'Name',
  exception_type: 'Exception',
  hostname: 'Worker',
  queue: 'Queue',
  task_group: 'Group',
  tag: 'Tag',
  time: 'Time',
}

export const FIELDS = Object.keys(FIELD_LABEL) as FilterField[]

/** A state as the word shown for it: `FAILURE` is "Failure". */
export const stateWord = (state: string): string => state.charAt(0) + state.slice(1).toLowerCase()

const MAX_SUGGESTIONS = 40

interface FilterPopoverProps {
  /** The field being edited, or null for the list of fields. */
  field: FilterField | null
  /** For the Name editor: edit the exact name instead of the substring. */
  exact: boolean
  filters: ExecutionFilters
  suggestions: FilterSuggestions
  /** Applies the filters; `close` ends the edit (a text value is one edit, state toggles stay open). */
  onCommit: (next: ExecutionFilters, close: boolean) => void
  onPickField: (field: FilterField) => void
}

const control =
  'h-[28px] w-full rounded-[3px] bg-raised px-2 text-xs text-ink placeholder:text-muted'
const itemButton =
  'block w-full truncate rounded-[2px] px-2 py-1 text-left text-xs text-ink hover:bg-hover'

/** The body of the filter popover: the field list, or the editor for one field. */
export function FilterPopover(props: FilterPopoverProps): ReactElement {
  const { field } = props
  if (field === null) return <FieldList {...props} />
  if (field === 'state') return <StateEditor {...props} />
  if (field === 'time') return <TimeEditor {...props} />
  if (field === 'name') return <NameEditor {...props} />
  return <TextEditor {...props} field={field} />
}

function FieldList({ onPickField }: FilterPopoverProps): ReactElement {
  return (
    <div className="flex flex-col gap-0.5">
      {FIELDS.map((field) => (
        <button key={field} type="button" className={itemButton} onClick={() => onPickField(field)}>
          {FIELD_LABEL[field]}
        </button>
      ))}
    </div>
  )
}

function StateEditor({ filters, onCommit }: FilterPopoverProps): ReactElement {
  const chosen = filters.state ? filters.state.split(',') : []
  const toggle = (state: string, on: boolean) => {
    // New states go at the end: the URL keeps the order they were picked in
    const next = on ? [...chosen, state] : chosen.filter((s) => s !== state)
    onCommit({ ...filters, state: next.length ? next.join(',') : undefined }, false)
  }
  return (
    <div className="flex flex-col gap-0.5">
      {TASK_STATES.map((state) => (
        <label
          key={state}
          className="flex cursor-pointer items-center gap-2 rounded-[2px] px-2 py-1 text-xs text-ink hover:bg-hover"
        >
          <input
            type="checkbox"
            checked={chosen.includes(state)}
            onChange={(event) => toggle(state, event.target.checked)}
            className="size-3.5 accent-[var(--ink)]"
          />
          {stateWord(state)}
        </label>
      ))}
    </div>
  )
}

function NameEditor({ filters, exact: startExact, onCommit }: FilterPopoverProps): ReactElement {
  const [exact, setExact] = useState(startExact)
  const [text, setText] = useState((startExact ? filters.name_exact : filters.name) ?? '')
  const id = useId()
  const apply = () => {
    const value = text.trim() || undefined
    onCommit(exact ? { ...filters, name_exact: value } : { ...filters, name: value }, true)
  }
  return (
    <form
      className="flex flex-col gap-2 p-1"
      onSubmit={(event) => {
        event.preventDefault()
        apply()
      }}
    >
      <label htmlFor={id} className="text-xs text-muted">
        {exact ? 'Task name' : 'Name contains'}
      </label>
      <input
        id={id}
        type="text"
        value={text}
        onChange={(event) => setText(event.target.value)}
        placeholder={exact ? 'app.module.task_name' : 'Part of a task name'}
        className={`${control} font-mono`}
      />
      <label className="flex cursor-pointer items-center gap-2 text-xs text-ink">
        <input
          type="checkbox"
          checked={exact}
          onChange={(event) => {
            setExact(event.target.checked)
            setText((event.target.checked ? filters.name_exact : filters.name) ?? text)
          }}
          className="size-3.5 accent-[var(--ink)]"
        />
        Exact name
      </label>
      <ApplyButton />
    </form>
  )
}

function TextEditor({
  field,
  filters,
  suggestions,
  onCommit,
}: FilterPopoverProps & {
  field: Exclude<FilterField, 'state' | 'name' | 'time'>
}): ReactElement {
  const [text, setText] = useState(filters[field] ?? '')
  const id = useId()
  const wanted = text.trim().toLowerCase()
  const offered = suggestions[field]
    .filter((value) => value.toLowerCase().includes(wanted))
    .slice(0, MAX_SUGGESTIONS)
  const apply = (value: string) =>
    onCommit({ ...filters, [field]: value.trim() || undefined }, true)
  return (
    <form
      className="flex flex-col gap-2 p-1"
      onSubmit={(event) => {
        event.preventDefault()
        apply(text)
      }}
    >
      <label htmlFor={id} className="text-xs text-muted">
        {FIELD_LABEL[field]}
      </label>
      <input
        id={id}
        type="text"
        value={text}
        onChange={(event) => setText(event.target.value)}
        className={`${control} font-mono`}
      />
      {offered.length > 0 && (
        <div className="flex max-h-48 flex-col overflow-y-auto">
          {offered.map((value) => (
            <button
              key={value}
              type="button"
              className={`${itemButton} font-mono`}
              onClick={() => apply(value)}
            >
              {value}
            </button>
          ))}
        </div>
      )}
      <ApplyButton />
    </form>
  )
}

function ApplyButton(): ReactElement {
  return (
    <button
      type="submit"
      className="h-[26px] self-start rounded-[3px] bg-ink px-3 text-xs font-semibold text-on-ink"
    >
      Apply
    </button>
  )
}

/** A preset's dates, counted back from today in the viewer's time zone. */
const PRESETS: { label: string; from: (today: Date) => Date; to: (today: Date) => Date }[] = [
  { label: 'Today', from: (d) => d, to: (d) => d },
  { label: 'Yesterday', from: (d) => subDays(d, 1), to: (d) => subDays(d, 1) },
  { label: 'Last 7 days', from: (d) => subDays(d, 6), to: (d) => d },
  { label: 'Last 30 days', from: (d) => subDays(d, 29), to: (d) => d },
]

/** How long a date has to stay as typed before it counts as finished (a person typing a year). */
const DATE_SETTLE_MS = 400
/** The oldest year a date typed digit by digit may have to commit by itself: `0202` is not a year. */
const EARLIEST_AUTO_YEAR = 1970

function TimeEditor({ filters, onCommit }: FilterPopoverProps): ReactElement {
  return (
    <div className="flex flex-col gap-2 p-1">
      <div className="flex flex-wrap gap-1">
        {PRESETS.map((preset) => (
          <button
            key={preset.label}
            type="button"
            className="h-[26px] rounded-[3px] bg-raised px-2.5 text-xs font-semibold text-ink hover:bg-press"
            onClick={() => {
              const today = new Date()
              onCommit(
                {
                  ...filters,
                  from_date: formatDateParam(preset.from(today)),
                  to_date: formatDateParam(preset.to(today)),
                },
                true,
              )
            }}
          >
            {preset.label}
          </button>
        ))}
      </div>
      <DateField
        label="From"
        committed={filters.from_date}
        onCommit={(value) => onCommit({ ...filters, from_date: value }, false)}
      />
      <DateField
        label="To"
        committed={filters.to_date}
        onCommit={(value) => onCommit({ ...filters, to_date: value }, false)}
      />
    </div>
  )
}

interface DateFieldProps {
  label: string
  /** The date in the URL, a finished YYYY-MM-DD or nothing. */
  committed: string | undefined
  onCommit: (value: string | undefined) => void
}

/**
 * A date input whose typing stays local. A date input reports a value on every digit of the
 * year (2025 passes through 0002, 0020 and 0202), so what is typed is only committed when it is
 * a finished date: on Enter or when the field loses focus, or once it has settled as a valid
 * date from 1970 on. An unfinished or rejected entry is never written to the URL, and leaving
 * the field with one puts the committed date back. Escape cancels an edit before it closes
 * the popover.
 */
function DateField({ label, committed, onCommit }: DateFieldProps): ReactElement {
  const id = useId()
  const [text, setText] = useState(committed ?? '')
  // Remounts the input to drop segments typed so far, which a controlled value can't reset
  const [epoch, setEpoch] = useState(0)
  const timer = useRef<number | undefined>(undefined)
  // The latest props, for a timer that fires after the page has moved on: a stale `onCommit`
  // would write the other date's old value back
  const latest = useRef({ committed, onCommit })
  latest.current = { committed, onCommit }

  // A date set from outside (a preset) shows here too
  useEffect(() => setText(committed ?? ''), [committed])
  useEffect(() => () => window.clearTimeout(timer.current), [])

  const revert = () => {
    window.clearTimeout(timer.current)
    setText(committed ?? '')
    setEpoch((n) => n + 1)
  }

  /** Commits what is in the input if it is finished; an unfinished entry is put back. */
  const settle = (input: HTMLInputElement) => {
    window.clearTimeout(timer.current)
    if (input.validity.badInput) return revert()
    const value = input.value
    if (value === '') {
      if (committed) onCommit(undefined)
      return
    }
    const valid = dateSearchParam(value)
    if (valid === undefined) return revert()
    if (valid !== committed) onCommit(valid)
  }

  return (
    <>
      <label htmlFor={id} className="text-xs text-muted">
        {label}
      </label>
      <input
        key={epoch}
        id={id}
        type="date"
        value={text}
        onChange={(event) => {
          const input = event.target
          setText(input.value)
          window.clearTimeout(timer.current)
          const valid = dateSearchParam(input.value)
          if (valid === undefined || Number(valid.slice(0, 4)) < EARLIEST_AUTO_YEAR) return
          timer.current = window.setTimeout(() => {
            if (valid !== latest.current.committed) latest.current.onCommit(valid)
          }, DATE_SETTLE_MS)
        }}
        onBlur={(event) => settle(event.currentTarget)}
        onKeyDown={(event) => {
          if (event.key === 'Enter') {
            event.preventDefault()
            settle(event.currentTarget)
          } else if (
            event.key === 'Escape' &&
            (event.currentTarget.validity.badInput || text !== (committed ?? ''))
          ) {
            // Cancel the edit; the next Escape closes the popover
            event.stopPropagation()
            revert()
          }
        }}
        className={control}
      />
    </>
  )
}
