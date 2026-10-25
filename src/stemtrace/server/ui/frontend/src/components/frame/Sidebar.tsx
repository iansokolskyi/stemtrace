import type { ReactElement, Ref } from 'react'
import { useHealth } from '@/api/queries'
import { LogoutButton } from '@/components/LogoutButton'
import { SproutMark } from '@/components/SproutMark'
import { useProblemCounts } from '@/hooks/useProblemCounts'
import { LiveStatus } from './LiveStatus'
import { NavItem } from './NavItem'
import { ThemeToggle } from './ThemeToggle'

interface SidebarProps {
  /** Below 900px the sidebar is an off-canvas drawer. */
  isDrawer: boolean
  open: boolean
  onNavigate: () => void
  ref?: Ref<HTMLElement>
}

/** Fixed navigation: brand, the pages with their problem badges, and the live/theme footer. */
export function Sidebar({ isDrawer, open, onNavigate, ref }: SidebarProps): ReactElement {
  const { offlineWorkers, notRegistered } = useProblemCounts()
  const { data: health } = useHealth()
  const hidden = isDrawer && !open

  return (
    <aside
      ref={ref}
      id="sidebar"
      aria-label="Primary navigation"
      inert={hidden}
      className={`z-40 flex h-dvh flex-col bg-surface ${
        isDrawer
          ? `fixed inset-y-0 left-0 w-[272px] max-w-[86vw] shadow-pop transition-transform duration-200 ${open ? 'translate-x-0' : 'invisible -translate-x-[102%]'}`
          : 'sticky top-0 self-start'
      }`}
    >
      <div className="flex items-center gap-2.5 px-4 pt-4 pb-3">
        <SproutMark size={30} className="flex-none" />
        <span className="text-base font-extrabold tracking-[-0.01em]">stemtrace</span>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto px-2.5 pt-2 pb-3">
        <nav aria-label="Sections" className="flex flex-col gap-0.5">
          <NavItem to="/executions" label="Executions" icon="list" onNavigate={onNavigate} />
          <NavItem to="/flows" label="Flows" icon="flow" onNavigate={onNavigate} />
          <NavItem
            to="/workers"
            label="Workers"
            icon="server"
            onNavigate={onNavigate}
            badge={
              offlineWorkers ? { text: `${offlineWorkers} offline`, tone: 'failure' } : undefined
            }
          />
          <NavItem
            to="/registry"
            label="Registry"
            icon="book"
            onNavigate={onNavigate}
            badge={
              notRegistered
                ? { text: `${notRegistered} not registered`, tone: 'anomaly' }
                : undefined
            }
          />
        </nav>
      </div>
      <div className="flex flex-wrap items-center gap-2.5 px-4 pt-2.5 pb-3.5">
        <LiveStatus />
        <span className="flex-1" />
        <ThemeToggle />
        <LogoutButton />
        {health?.version && (
          <span className="w-full text-[11px] leading-snug text-muted">
            Version {health.version}
          </span>
        )}
      </div>
    </aside>
  )
}
