import { clsx } from 'clsx'
import type { ReactElement } from 'react'

interface TracebackToggleProps {
  open: boolean
  onToggle: () => void
  /** Spacing from the caller's layout; the look is fixed. */
  className?: string
}

/** The one "Show traceback" control: a button, wherever a traceback can be opened. */
export function TracebackToggle({ open, onToggle, className }: TracebackToggleProps): ReactElement {
  return (
    <button
      type="button"
      aria-expanded={open}
      onClick={onToggle}
      className={clsx(
        'inline-flex h-[26px] items-center rounded-[3px] bg-raised px-2.5 text-xs font-semibold hover:bg-press',
        className,
      )}
    >
      {open ? 'Hide traceback' : 'Show traceback'}
    </button>
  )
}
