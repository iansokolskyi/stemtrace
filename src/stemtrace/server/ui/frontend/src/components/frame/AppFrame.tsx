import { useRouterState } from '@tanstack/react-router'
import { type ReactElement, type ReactNode, useCallback, useEffect, useRef, useState } from 'react'
import { useMediaQuery } from '@/hooks/useMediaQuery'
import { Sidebar } from './Sidebar'
import { TopStrip } from './TopStrip'

/** The Signal box frame: fixed sidebar (a drawer below 900px), sticky top strip, the page. */
export function AppFrame({ children }: { children: ReactNode }): ReactElement {
  const isDrawer = useMediaQuery('(max-width: 899px)')
  const [drawerOpen, setDrawerOpen] = useState(false)
  const menuButtonRef = useRef<HTMLButtonElement>(null)
  const sidebarRef = useRef<HTMLElement>(null)
  const pathname = useRouterState({ select: (s) => s.location.pathname })

  const closeDrawer = useCallback((restoreFocus: boolean) => {
    setDrawerOpen(false)
    if (restoreFocus) menuButtonRef.current?.focus()
  }, [])

  // Navigating, or widening past the breakpoint, closes the drawer.
  // biome-ignore lint/correctness/useExhaustiveDependencies: pathname is the trigger, not an input
  useEffect(() => setDrawerOpen(false), [pathname, isDrawer])

  useEffect(() => {
    if (!drawerOpen) return
    sidebarRef.current?.querySelector<HTMLElement>('a, button')?.focus()
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') closeDrawer(true)
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [drawerOpen, closeDrawer])

  return (
    <div className="grid min-h-dvh grid-cols-[minmax(0,1fr)] min-[900px]:grid-cols-[244px_minmax(0,1fr)]">
      <Sidebar
        ref={sidebarRef}
        isDrawer={isDrawer}
        open={drawerOpen}
        onNavigate={() => closeDrawer(false)}
      />
      {isDrawer && drawerOpen && (
        <div
          aria-hidden="true"
          className="fixed inset-0 z-35 bg-scrim"
          onClick={() => closeDrawer(true)}
        />
      )}
      <div className="min-w-0">
        <TopStrip
          isDrawer={isDrawer}
          drawerOpen={drawerOpen}
          onOpenMenu={() => setDrawerOpen(true)}
          menuButtonRef={menuButtonRef}
        />
        <main id="main" className="max-w-[1500px] px-4 pt-5 pb-14 xl:px-6">
          {children}
        </main>
      </div>
    </div>
  )
}
