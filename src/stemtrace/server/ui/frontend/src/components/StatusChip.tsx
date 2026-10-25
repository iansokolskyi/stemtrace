import { clsx } from 'clsx'
import type { ReactElement } from 'react'
import { Icon, type IconName } from './Icon'

interface Look {
  word: string
  icon: IconName
  /** Normal states are quiet; only failure and retry get a filled block and bold. */
  loud: boolean
  /** Fill of the loud icon block (full class names, so Tailwind sees them). */
  block: string
}

const PENDING: Look = { word: 'Pending', icon: 'clock', loud: false, block: '' }

const LOOKS: Record<string, Look> = {
  SUCCESS: { word: 'Success', icon: 'check', loud: false, block: '' },
  FAILURE: { word: 'Failure', icon: 'x', loud: true, block: 'bg-failure text-on-failure' },
  // Retry is always hatched at 45 degrees with 2px surface gaps (see .retry-hatch in index.css).
  RETRY: { word: 'Retry', icon: 'retry', loud: true, block: 'retry-hatch text-on-retry' },
  STARTED: { word: 'Running', icon: 'play', loud: false, block: '' },
  RECEIVED: { word: 'Received', icon: 'down', loud: false, block: '' },
  PENDING,
  REVOKED: { word: 'Revoked', icon: 'minus', loud: false, block: '' },
  REJECTED: { word: 'Rejected', icon: 'x', loud: false, block: '' },
  IGNORED: { word: 'Ignored', icon: 'minus', loud: false, block: '' },
}

function lookOf(state: string): Look {
  const known = LOOKS[state]
  if (known) return known
  const lower = state.toLowerCase()
  return { ...PENDING, word: lower.charAt(0).toUpperCase() + lower.slice(1) }
}

interface StatusChipProps {
  /** The Celery state, for example `FAILURE`. */
  state: string
  /** Hover text, for example the exception type of a failure. */
  title?: string
  className?: string
}

/**
 * A task state as icon plus word, never colour alone. Success, running and the other normal
 * states are quiet (muted regular text, outline icon); failure and retry are loud (a filled
 * icon block and bold ink text).
 */
export function StatusChip({ state, title, className }: StatusChipProps): ReactElement {
  const look = lookOf(state)
  return (
    <span
      data-state={state}
      data-variant={look.loud ? 'loud' : 'quiet'}
      title={title}
      className={clsx(
        'inline-flex h-5 items-center gap-1.5 whitespace-nowrap text-xs',
        look.loud ? 'font-bold text-ink' : 'font-normal text-muted',
        className,
      )}
    >
      <span
        data-status-icon
        className={clsx(
          'inline-flex size-4 shrink-0 items-center justify-center rounded-[3px]',
          look.loud && look.block,
        )}
      >
        <Icon
          name={look.icon}
          size={look.loud ? 10 : 14}
          className={look.loud ? '[stroke-width:3]' : ''}
        />
      </span>
      {look.word}
    </span>
  )
}
