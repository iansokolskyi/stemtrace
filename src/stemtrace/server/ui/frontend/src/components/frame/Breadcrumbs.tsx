import { Link, useMatches } from '@tanstack/react-router'
import type { ReactElement } from 'react'
import { Icon } from '@/components/Icon'
import { useCarriedRange } from '@/hooks/useCarriedRange'

/**
 * Where you are: the page's crumb from its route's staticData, and on a detail page the list
 * it belongs to (linked) followed by the item's ID or name.
 */
export function Breadcrumbs(): ReactElement {
  const carried = useCarriedRange()
  const match = useMatches({ select: (matches) => matches[matches.length - 1] })
  const { crumb, section } = match?.staticData ?? {}
  const detail = Object.values((match?.params ?? {}) as Record<string, string>)[0]

  return (
    <nav
      aria-label="Breadcrumb"
      className="flex min-w-0 flex-1 items-center gap-1.5 overflow-hidden whitespace-nowrap text-[13px]"
    >
      {section && detail ? (
        <>
          <Link
            to={section.to}
            search={
              (section.to === '/executions' || section.to === '/flows' ? carried : {}) as never
            }
            className="text-muted hover:text-ink hover:underline"
          >
            {section.label}
          </Link>
          <Icon name="chev" size={12} className="flex-none text-muted" />
          <span
            aria-current="page"
            title={detail}
            className="min-w-0 overflow-hidden text-ellipsis font-mono text-xs font-bold"
          >
            {detail}
          </span>
        </>
      ) : (
        crumb && (
          <span aria-current="page" className="font-bold">
            {crumb}
          </span>
        )
      )}
    </nav>
  )
}
