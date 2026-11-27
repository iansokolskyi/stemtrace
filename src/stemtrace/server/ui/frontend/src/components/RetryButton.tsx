import { type ReactElement, useRef, useState } from 'react'

/**
 * A Retry that stays put while it works: the button reads "Retrying…" and ignores clicks (it is
 * `aria-disabled`, not `disabled`, so keyboard focus stays on it). When the retry ends with the
 * button still there (it failed again) focus returns to it; when the button went away with the
 * error, focus moves to the page heading instead of dropping onto the body.
 */
export function RetryButton({
  retry,
  busy = false,
}: {
  retry: () => unknown
  /** A request is already in flight (a poll), so the button shows it too. */
  busy?: boolean
}): ReactElement {
  const [clicked, setClicked] = useState(false)
  const buttonRef = useRef<HTMLButtonElement | null>(null)
  const working = clicked || busy
  const run = async (): Promise<void> => {
    if (working) return
    setClicked(true)
    // Where focus goes if the button goes away with the error: the heading of what it sat in
    const panel = buttonRef.current?.closest('[data-testid="triage-panel"]')
    try {
      await retry()
    } finally {
      setClicked(false)
      requestAnimationFrame(() => {
        if (buttonRef.current?.isConnected) buttonRef.current.focus()
        else if (document.activeElement === document.body) {
          const heading =
            panel?.querySelector<HTMLElement>('h2') ?? document.querySelector<HTMLElement>('h1')
          heading?.focus()
        }
      })
    }
  }
  return (
    <button
      ref={buttonRef}
      type="button"
      aria-disabled={working || undefined}
      onClick={run}
      className="h-6 rounded-[3px] bg-raised px-2.5 text-small font-medium hover:bg-press aria-disabled:cursor-progress aria-disabled:text-muted"
    >
      {working ? 'Retrying…' : 'Retry'}
    </button>
  )
}
