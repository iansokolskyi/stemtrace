import { Link } from '@tanstack/react-router'
import type { ReactElement } from 'react'
import { Icon, type IconName } from '@/components/Icon'
import { useCarriedRange } from '@/hooks/useCarriedRange'

interface NavItemProps {
  to: '/executions' | '/flows' | '/workers' | '/registry'
  label: string
  icon: IconName
  /** A problem badge ("1 offline"); omitted when there is nothing wrong or the count is unknown. */
  badge?: { text: string; tone: 'failure' | 'anomaly' }
  onNavigate?: () => void
}

const BADGE = {
  failure: 'bg-failure text-on-failure',
  anomaly: 'bg-anomaly text-on-anomaly',
}

/** One sidebar destination; the current page gets aria-current and the selected fill. */
export function NavItem({ to, label, icon, badge, onNavigate }: NavItemProps): ReactElement {
  const carried = useCarriedRange()
  return (
    <Link
      to={to}
      // Only Executions has a range; the other pages' search types have no such key
      search={(to === '/executions' ? carried : {}) as never}
      onClick={onNavigate}
      activeOptions={{ includeSearch: false }}
      className="nav-item flex items-center gap-2.5 rounded-[3px] px-2.5 py-[7px] font-semibold text-muted hover:bg-hover hover:text-ink"
    >
      <Icon name={icon} size={16} />
      <span>{label}</span>
      {badge && (
        <span
          className={`ml-auto whitespace-nowrap rounded-[3px] px-1.5 py-px text-[11px] font-bold ${BADGE[badge.tone]}`}
        >
          {badge.text}
        </span>
      )}
    </Link>
  )
}
