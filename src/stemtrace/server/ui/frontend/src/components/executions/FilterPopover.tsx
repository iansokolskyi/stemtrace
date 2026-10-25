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

function TimeEditor({ filters, onCommit }: FilterPopoverProps): ReactElement {
  const fromId = useId()
  const toId = useId()
  // `type=date` gives '' or a real YYYY-MM-DD; dateSearchParam is the same rule the URL uses
  const set = (key: 'from_date' | 'to_date', value: string, close: boolean) =>
    onCommit({ ...filters, [key]: dateSearchParam(value) }, close)
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
      <label htmlFor={fromId} className="text-xs text-muted">
        From
      </label>
      <input
        id={fromId}
        type="date"
        value={filters.from_date ?? ''}
        onChange={(event) => set('from_date', event.target.value, false)}
        className={control}
      />
      <label htmlFor={toId} className="text-xs text-muted">
        To
      </label>
      <input
        id={toId}
        type="date"
        value={filters.to_date ?? ''}
        onChange={(event) => set('to_date', event.target.value, false)}
        className={control}
      />
    </div>
  )
}
