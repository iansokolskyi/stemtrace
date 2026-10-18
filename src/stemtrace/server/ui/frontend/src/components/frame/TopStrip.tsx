import { useCanGoBack, useRouter } from '@tanstack/react-router'
import type { ReactElement, Ref } from 'react'
import { Icon } from '@/components/Icon'
import { Breadcrumbs } from './Breadcrumbs'

interface TopStripProps {
  isDrawer: boolean
  drawerOpen: boolean
  onOpenMenu: () => void
  menuButtonRef?: Ref<HTMLButtonElement>
}

const ICON_BUTTON =
  'inline-flex size-8 flex-none items-center justify-center rounded-[3px] bg-raised hover:enabled:bg-press disabled:cursor-not-allowed disabled:opacity-50'

/** Sticky strip above every page: menu (narrow screens), Back and the breadcrumb. */
export function TopStrip({
  isDrawer,
  drawerOpen,
  onOpenMenu,
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
    </header>
  )
}
