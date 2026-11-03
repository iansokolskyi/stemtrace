import { useCanGoBack, useRouter } from '@tanstack/react-router'
import type { ReactElement, Ref } from 'react'
import { Icon } from '@/components/Icon'
import { Breadcrumbs } from './Breadcrumbs'
import { TimeRange } from './TimeRange'

interface TopStripProps {
  isDrawer: boolean
  drawerOpen: boolean
  onOpenMenu: () => void
  onOpenSearch: () => void
  menuButtonRef?: Ref<HTMLButtonElement>
}

/** The shortcut as the platform writes it. */
const SHORTCUT = /Mac|iPhone|iPad/.test(navigator.platform) ? '⌘K' : 'Ctrl K'

const ICON_BUTTON =
  'inline-flex size-8 flex-none items-center justify-center rounded-[3px] bg-raised hover:enabled:bg-press disabled:cursor-not-allowed disabled:opacity-50'

/** Sticky strip above every page: menu (narrow screens), Back, the breadcrumb and the time range. */
export function TopStrip({
  isDrawer,
  drawerOpen,
  onOpenMenu,
  onOpenSearch,
  menuButtonRef,
}: TopStripProps): ReactElement {
  const router = useRouter()
  const canGoBack = useCanGoBack()

  return (
    <header className="sticky top-0 z-30 flex min-h-[52px] flex-wrap items-center gap-2 bg-ground px-4 py-2 min-[900px]:flex-nowrap xl:px-6">
      {isDrawer && (
        <button
          ref={menuButtonRef}
          type="button"
          onClick={onOpenMenu}
          aria-label="Open navigation"
          aria-controls="sidebar"
          aria-expanded={drawerOpen}
          className={ICON_BUTTON}
        >
          <Icon name="menu" size={18} />
        </button>
      )}
      <button
        type="button"
        onClick={() => router.history.back()}
        disabled={!canGoBack}
        aria-label="Back"
        title={canGoBack ? 'Back' : 'No earlier page'}
        className={ICON_BUTTON}
      >
        <Icon name="back" size={16} />
      </button>
      <Breadcrumbs />
      <button
        type="button"
        onClick={onOpenSearch}
        aria-label="Search (Ctrl or Command K)"
        className="inline-flex h-8 min-w-0 flex-none items-center gap-2 rounded-[3px] bg-raised px-[9px] text-muted hover:bg-press hover:text-ink min-[900px]:min-w-[230px] min-[900px]:pr-2 min-[900px]:pl-2.5"
      >
        <Icon name="search" size={14} />
        <span className="hidden min-[900px]:inline">Search tasks, flows, workers</span>
        <kbd className="ml-auto hidden rounded-[2px] bg-ground px-[5px] py-px font-[inherit] text-[11px] text-muted min-[900px]:inline">
          {SHORTCUT}
        </kbd>
      </button>
      <TimeRange />
    </header>
  )
}
