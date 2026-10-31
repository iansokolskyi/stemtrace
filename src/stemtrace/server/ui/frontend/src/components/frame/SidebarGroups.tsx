import { Link } from '@tanstack/react-router'
import type { ReactElement } from 'react'
import { useGroups } from '@/api/queries'
import { useCarriedRange } from '@/hooks/useCarriedRange'

const LINK =
  'block truncate rounded-[3px] px-2.5 py-[5px] text-[13px] text-muted hover:bg-hover hover:text-ink aria-[current=page]:bg-raised aria-[current=page]:font-semibold aria-[current=page]:text-ink'

/**
 * The Groups and Tags sections of the sidebar, from `/api/groups`. Each entry opens Executions
 * filtered by it, replacing the filters already set (it is a jump, not an addition). Nothing is
 * drawn when the server has no `/api/groups` (the request fails) or no groups. No failure rate
 * yet: that needs per-group statistics the server doesn't serve.
 */
export function SidebarGroups({ onNavigate }: { onNavigate?: () => void }): ReactElement | null {
  const { data } = useGroups()
  const carried = useCarriedRange()
  const groups = data?.groups ?? []
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
            {group.name}
          </Link>
        ))}
      </nav>
      {tags.length > 0 && (
        <nav aria-label="Tags" className="mt-4 flex flex-col gap-0.5">
          <p className="px-2.5 pb-1 text-xs font-semibold text-muted">Tags</p>
          {tags.map((tag) => (
            <Link
              key={tag}
              to="/executions"
              search={{ tag, ...carried }}
              onClick={onNavigate}
              className={LINK}
            >
              #{tag}
            </Link>
          ))}
        </nav>
      )}
    </>
  )
}
