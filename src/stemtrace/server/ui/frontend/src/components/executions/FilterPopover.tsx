import { subDays } from 'date-fns'
import { type ReactElement, useId, useState } from 'react'
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

/** In the order "Filter by" lists them: the prototype's six, then the fields it has no menu for. */
export const FIELD_LABEL: Record<FilterField, string> = {
  state: 'State',
  task_group: 'Group',
  name: 'Task name',
  hostname: 'Worker',
  queue: 'Queue',
  time: 'Time range',
  exception_type: 'Exception',
  tag: 'Tag',
}

export const FIELDS = Object.keys(FIELD_LABEL) as FilterField[]

/** A state as the word shown for it: `FAILURE` is "Failure", `STARTED` "Running" (as its chip). */
export const stateWord = (state: string): string =>
  state === 'STARTED' ? 'Running' : state.charAt(0) + state.slice(1).toLowerCase()

/** The states the editor offers: the ones to look at first, then the rest in lifecycle order. */
const LOOK_FIRST = ['FAILURE', 'RETRY', 'STARTED', 'PENDING', 'SUCCESS']
const STATE_ORDER = [...LOOK_FIRST, ...TASK_STATES.filter((state) => !LOOK_FIRST.includes(state))]

const MAX_SUGGESTIONS = 40

interface FilterPopoverProps {
  /** The field being edited, or null for the list of fields. */
  field: FilterField | null
  /** For the Name editor: edit the exact name instead of the substring. */
  exact: boolean
  filters: ExecutionFilters
  suggestions: FilterSuggestions
  /**
   * Sets some filters on top of the latest ones; `close` ends the edit (a text value is one
   * edit, state toggles stay open).
   */
  onCommit: (patch: ExecutionFilters, close: boolean) => void
  /** Ends the edit without changing anything. */
  onClose: () => void
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
      <p className="m-0 px-2 pt-1.5 pb-1 text-xs font-bold text-muted">Filter by</p>
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
    onCommit({ state: next.length ? next.join(',') : undefined }, false)
  }
  return (
    <div className="flex flex-col gap-0.5">
      {STATE_ORDER.map((state) => (
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
          <span>{stateWord(state)}</span>
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
    onCommit(exact ? { name_exact: value } : { name: value }, true)
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
        <span>Exact name</span>
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
  const apply = (value: string) => onCommit({ [field]: value.trim() || undefined }, true)
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

function ApplyButton({ disabled = false }: { disabled?: boolean }): ReactElement {
  return (
    <button
      type="submit"
      disabled={disabled}
      className="h-[26px] self-start rounded-[3px] bg-ink px-3 text-xs font-semibold text-on-ink disabled:cursor-not-allowed disabled:opacity-40"
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

/** What a date input holds: its text, and whether it is only partly typed. */
interface DateEntry {
  value: string
  /** The browser has some segments of a date but not a whole one (`validity.badInput`). */
  unfinished: boolean
}

/** Whether an entry is empty or a finished date `dateSearchParam` accepts. */
const entryIsValid = (entry: DateEntry): boolean =>
  !entry.unfinished && (entry.value === '' || dateSearchParam(entry.value) !== undefined)

/**
 * The Time editor. A preset applies at once. Typed dates stay here, in this popover's own
 * state, and are applied only by Apply or by Enter in a date field, as one edit, and only when
 * both are empty or finished valid dates. Every other way of leaving drops them: the popover
 * closing unmounts this, so nothing typed can reach the URL by itself (a date input reports a
 * value on every digit of the year, so applying as it goes would apply 0002, 0020, 0202).
 */
function TimeEditor({ filters, onCommit, onClose }: FilterPopoverProps): ReactElement {
  const [from, setFrom] = useState<DateEntry>({ value: filters.from_date ?? '', unfinished: false })
  const [to, setTo] = useState<DateEntry>({ value: filters.to_date ?? '', unfinished: false })
  const valid = entryIsValid(from) && entryIsValid(to)

  const apply = () => {
    if (!valid) return
    const next = { from_date: dateSearchParam(from.value), to_date: dateSearchParam(to.value) }
    if (next.from_date === filters.from_date && next.to_date === filters.to_date) onClose()
    else onCommit(next, true)
  }

  return (
    <form
      className="flex flex-col gap-2 p-1"
      onSubmit={(event) => {
        event.preventDefault()
        apply()
      }}
    >
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
      <DateInput label="From" entry={from} onEntry={setFrom} />
      <DateInput label="To" entry={to} onEntry={setTo} />
      <ApplyButton disabled={!valid} />
    </form>
  )
}

interface DateInputProps {
  label: string
  entry: DateEntry
  onEntry: (entry: DateEntry) => void
}

/** A date input that only reports what it holds; applying is the editor's business. */
function DateInput({ label, entry, onEntry }: DateInputProps): ReactElement {
  const id = useId()
  const report = (input: HTMLInputElement) => {
    const unfinished = input.validity.badInput
    if (input.value !== entry.value || unfinished !== entry.unfinished) {
      onEntry({ value: input.value, unfinished })
    }
  }
  return (
    <>
      <label htmlFor={id} className="text-xs text-muted">
        {label}
      </label>
      <input
        id={id}
        type="date"
        // Not controlled: while a date is half typed the browser's own value is the truth
        defaultValue={entry.value}
        // A date typed from nothing to "month only" changes no value, so no change event says so:
        // the key that typed it is the signal
        onChange={(event) => report(event.target)}
        onKeyUp={(event) => report(event.currentTarget)}
        onBlur={(event) => report(event.currentTarget)}
        aria-invalid={!entryIsValid(entry)}
        className={control}
      />
    </>
  )
}
