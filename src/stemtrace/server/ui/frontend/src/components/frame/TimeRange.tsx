import { useMatches, useNavigate } from '@tanstack/react-router'
import type { ReactElement } from 'react'
import { DEFAULT_RANGE, effectiveRange, type Range, rangeSearch } from '@/routes/-search'

const OPTIONS: readonly { value: Range; label: string }[] = [
  { value: '1h', label: '1h' },
  { value: '6h', label: '6h' },
  { value: 'all', label: 'All' },
]

/**
 * The time range switch (1h, 6h, All) in the top strip. It writes `range` to the URL of a page
 * whose route sets `staticData.timeRange`, and renders nothing on any other page.
 */
export function TimeRange(): ReactElement | null {
  const navigate = useNavigate()
  const match = useMatches({ select: (matches) => matches[matches.length - 1] })
  if (!match?.staticData.timeRange) return null

  const search = match.search as Record<string, unknown>
  const current = effectiveRange(rangeSearch(search).range, search)

  const choose = (range: Range) => {
    // Each choice is a history entry; the default is the absence of the param, and a range
    // replaces explicit dates. The current route is only known at run time, so the router types
    // its search as `never`: the cast is on what the reducer returns.
    navigate({
      search: (prev) =>
        ({
          ...prev,
          range: range === DEFAULT_RANGE ? undefined : range,
          from_date: undefined,
          to_date: undefined,
        }) as never,
    })
  }

  return (
    <fieldset
      aria-label="Time range"
      className="m-0 inline-flex min-w-0 flex-none gap-0.5 rounded-[3px] border-0 bg-raised p-0.5"
    >
      {OPTIONS.map(({ value, label }) => (
        <button
          key={value}
          type="button"
          aria-pressed={current === value}
          onClick={() => choose(value)}
          className="h-[26px] rounded-[2px] px-[11px] text-xs font-semibold text-muted hover:bg-hover hover:text-ink aria-pressed:bg-ink aria-pressed:text-on-ink aria-pressed:hover:bg-ink aria-pressed:hover:text-on-ink"
        >
          {label}
        </button>
      ))}
    </fieldset>
  )
}
