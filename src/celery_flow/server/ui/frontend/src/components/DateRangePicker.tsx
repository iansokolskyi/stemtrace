/**
 * Date range picker with preset ranges.
 */

import {
  endOfDay,
  endOfMonth,
  endOfWeek,
  format,
  startOfDay,
  startOfMonth,
  startOfWeek,
  subDays,
  subMonths,
  subWeeks,
} from 'date-fns'
import { useEffect, useRef, useState } from 'react'
import { type DateRange, DayPicker } from 'react-day-picker'

interface DateRangePickerProps {
  value: DateRange | undefined
  onChange: (range: DateRange | undefined) => void
}

interface Preset {
  label: string
  getValue: () => DateRange
}

const presets: Preset[] = [
  {
    label: 'Today',
    getValue: () => ({
      from: startOfDay(new Date()),
      to: endOfDay(new Date()),
    }),
  },
  {
    label: 'Yesterday',
    getValue: () => {
      const yesterday = subDays(new Date(), 1)
      return {
        from: startOfDay(yesterday),
        to: endOfDay(yesterday),
      }
    },
  },
  {
    label: 'This Week',
    getValue: () => ({
      from: startOfWeek(new Date(), { weekStartsOn: 1 }),
      to: endOfWeek(new Date(), { weekStartsOn: 1 }),
    }),
  },
  {
    label: 'Last Week',
    getValue: () => {
      const lastWeek = subWeeks(new Date(), 1)
      return {
        from: startOfWeek(lastWeek, { weekStartsOn: 1 }),
        to: endOfWeek(lastWeek, { weekStartsOn: 1 }),
      }
    },
  },
  {
    label: 'Last 7 Days',
    getValue: () => ({
      from: startOfDay(subDays(new Date(), 6)),
      to: endOfDay(new Date()),
    }),
  },
  {
    label: 'Last 30 Days',
    getValue: () => ({
      from: startOfDay(subDays(new Date(), 29)),
      to: endOfDay(new Date()),
    }),
  },
  {
    label: 'This Month',
    getValue: () => ({
      from: startOfMonth(new Date()),
      to: endOfMonth(new Date()),
    }),
  },
  {
    label: 'Last Month',
    getValue: () => {
      const lastMonth = subMonths(new Date(), 1)
      return {
        from: startOfMonth(lastMonth),
        to: endOfMonth(lastMonth),
      }
    },
  },
]

export function DateRangePicker({ value, onChange }: DateRangePickerProps) {
  const [isOpen, setIsOpen] = useState(false)
  const containerRef = useRef<HTMLDivElement>(null)

  // Close on click outside
  useEffect(() => {
    function handleClickOutside(event: MouseEvent) {
      if (containerRef.current && !containerRef.current.contains(event.target as Node)) {
        setIsOpen(false)
      }
    }
    document.addEventListener('mousedown', handleClickOutside)
    return () => document.removeEventListener('mousedown', handleClickOutside)
  }, [])

  const formatRange = () => {
    if (!value?.from) return 'Select date range'
    if (!value.to) return format(value.from, 'MMM d, yyyy')
    if (format(value.from, 'yyyy-MM-dd') === format(value.to, 'yyyy-MM-dd')) {
      return format(value.from, 'MMM d, yyyy')
    }
    return `${format(value.from, 'MMM d')} - ${format(value.to, 'MMM d, yyyy')}`
  }

  const handlePreset = (preset: Preset) => {
    onChange(preset.getValue())
    setIsOpen(false)
  }

  const handleClear = () => {
    onChange(undefined)
    setIsOpen(false)
  }

  return (
    <div ref={containerRef} className="relative">
      <button
        type="button"
        onClick={() => setIsOpen(!isOpen)}
        className={`flex items-center gap-2 px-3 py-1.5 text-sm rounded-lg border transition-colors whitespace-nowrap ${
          value?.from
            ? 'bg-slate-700 border-slate-600 text-slate-100'
            : 'bg-slate-800 border-slate-700 text-slate-400 hover:border-slate-600'
        }`}
      >
        <CalendarIcon />
        <span>{formatRange()}</span>
        {value?.from && (
          <button
            type="button"
            onClick={(e) => {
              e.stopPropagation()
              handleClear()
            }}
            className="ml-1 text-slate-500 hover:text-slate-300"
          >
            <XIcon />
          </button>
        )}
      </button>

      {isOpen && (
        <div className="absolute top-full mt-2 right-0 z-50 flex bg-slate-900 rounded-xl border border-slate-700 shadow-2xl overflow-hidden">
          {/* Presets sidebar */}
          <div className="w-36 border-r border-slate-700 py-2">
            {presets.map((preset) => (
              <button
                key={preset.label}
                type="button"
                onClick={() => handlePreset(preset)}
                className="w-full px-3 py-1.5 text-left text-sm text-slate-300 hover:bg-slate-800 hover:text-slate-100 transition-colors"
              >
                {preset.label}
              </button>
            ))}
          </div>

          {/* Calendar */}
          <div className="p-3 relative">
            <DayPicker
              mode="range"
              selected={value}
              onSelect={onChange}
              numberOfMonths={2}
              classNames={{
                root: 'text-slate-200',
                months: 'flex gap-4',
                month: 'space-y-3',
                month_caption: 'flex justify-center items-center h-8',
                caption_label: 'text-sm font-medium text-slate-200',
                nav: 'absolute top-3 left-0 right-0 flex justify-between px-1',
                button_previous:
                  'p-1 rounded hover:bg-slate-800 text-slate-400 hover:text-slate-200',
                button_next: 'p-1 rounded hover:bg-slate-800 text-slate-400 hover:text-slate-200',
                weekdays: 'flex',
                weekday: 'w-8 text-center text-xs font-medium text-slate-500',
                week: 'flex',
                day: 'w-8 h-8 text-center text-sm p-0',
                day_button:
                  'w-8 h-8 rounded-md hover:bg-slate-800 transition-colors text-slate-300 hover:text-slate-100 disabled:text-slate-600 disabled:hover:bg-transparent',
                selected: 'bg-emerald-600 text-white hover:bg-emerald-600',
                range_start: 'rounded-l-md',
                range_end: 'rounded-r-md',
                range_middle: 'bg-emerald-600/30 rounded-none',
                today: 'font-bold text-emerald-400',
                disabled: 'text-slate-600',
              }}
            />
          </div>
        </div>
      )}
    </div>
  )
}

function CalendarIcon() {
  return (
    <svg
      className="w-4 h-4"
      fill="none"
      viewBox="0 0 24 24"
      stroke="currentColor"
      aria-hidden="true"
    >
      <path
        strokeLinecap="round"
        strokeLinejoin="round"
        strokeWidth={2}
        d="M8 7V3m8 4V3m-9 8h10M5 21h14a2 2 0 002-2V7a2 2 0 00-2-2H5a2 2 0 00-2 2v12a2 2 0 002 2z"
      />
    </svg>
  )
}

function XIcon() {
  return (
    <svg
      className="w-3 h-3"
      fill="none"
      viewBox="0 0 24 24"
      stroke="currentColor"
      aria-hidden="true"
    >
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
    </svg>
  )
}
