import { Link } from '@tanstack/react-router'
import type { ReactElement } from 'react'
import { useGroupFailureRates, useGroups } from '@/api/queries'
import { useCarriedRange } from '@/hooks/useCarriedRange'
import { DEFAULT_RANGE, type Range, WINDOW_WORDS } from '@/routes/-search'
import { formatPercent } from '@/utils/format'

const CHIP =
  'rounded-[3px] bg-raised px-2 py-0.5 text-xs text-muted hover:bg-press hover:text-ink aria-[current=page]:font-semibold aria-[current=page]:text-ink aria-[current=page]:[outline-offset:-1.5px] aria-[current=page]:[outline:1.5px_solid_var(--ink)]'
const LINK =
  'flex items-center justify-between gap-2 rounded-[3px] px-2.5 py-[5px] text-[13px] text-muted hover:bg-hover hover:text-ink aria-[current=page]:bg-raised aria-[current=page]:font-semibold aria-[current=page]:text-ink'

/** Over this a group's failure rate is drawn as a failure. */
const HIGH_FAILURE_RATE = 0.01

/**
 * The Groups and Tags sections of the sidebar, from `/api/groups`. Each entry opens Executions
 * filtered by it, replacing the filters already set (it is a jump, not an addition). Nothing is
 * drawn when the server has no `/api/groups` (the request fails) or no groups. No failure rate
 * rate, over the carried range, is shown when `/api/stats/timeseries` answers.
 */
export function SidebarGroups({ onNavigate }: { onNavigate?: () => void }): ReactElement | null {
  const { data } = useGroups()
  const carried = useCarriedRange()
  const range = carried.range ?? DEFAULT_RANGE
  const groups = data?.groups ?? []
  // One request for every group, and none while there are no groups; the whole retention for
  // All. A failed request shows no rates.
  const { data: rates } = useGroupFailureRates(
    range === 'all' ? undefined : range,
    groups.length > 0,
  )
  if (groups.length === 0) return null
  const tags = [...new Set(groups.flatMap((group) => group.tags))].sort((a, b) =>
    a.localeCompare(b),
  )

  return (
    <>
      <nav aria-label="Groups" className="mt-4 flex flex-col gap-0.5">
        <p className="px-2.5 pb-1 text-xs font-semibold text-muted">Groups</p>
        {groups.map((group) => (
          <Link
            key={group.name}
            to="/executions"
            search={{ task_group: group.name, ...carried }}
            onClick={onNavigate}
            className={LINK}
          >
            <span className="min-w-0 truncate">{group.name}</span>
            <FailureRate rate={rates?.get(group.name)} range={range} />
          </Link>
        ))}
      </nav>
      {tags.length > 0 && (
        <nav aria-label="Tags" className="mt-4">
          <p className="px-2.5 pb-1 text-xs font-semibold text-muted">Tags</p>
          <div className="flex flex-wrap gap-1.5 px-2.5">
            {tags.map((tag) => (
              <Link
                key={tag}
                to="/executions"
                search={{ tag, ...carried }}
                onClick={onNavigate}
                className={CHIP}
              >
                #{tag}
              </Link>
            ))}
          </div>
        </nav>
      )}
    </>
  )
}

function FailureRate({
  rate,
  range,
}: {
  rate: number | undefined
  range: Range
}): ReactElement | null {
  if (rate === undefined) return null
  const window = WINDOW_WORDS[range]
  return (
    <span
      title={`Failure rate${window ? `, ${window}` : ''}`}
      className={
        rate > HIGH_FAILURE_RATE
          ? 'flex-none text-[11.5px] font-bold text-failure-t'
          : 'flex-none text-[11.5px] text-muted'
      }
    >
      {formatPercent(rate)}
    </span>
  )
}
