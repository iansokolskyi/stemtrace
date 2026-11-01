import { clsx } from 'clsx'
import type { ReactElement } from 'react'
import { Icon, type IconName } from './Icon'

interface StatusMarkProps {
  /** The status the element carries as `data-status`. */
  status: string
  word: string
  icon: IconName
  /** Loud: a filled icon block and bold ink. Quiet: muted regular text and an outline icon. */
  loud: boolean
  /** Fill of the loud icon block (full class names, so Tailwind sees them). */
  block: string
  testId: string
}

/** A page-specific status as icon plus word, never colour alone (Workers, Registry). */
export function StatusMark({
  status,
  word,
  icon,
  loud,
  block,
  testId,
}: StatusMarkProps): ReactElement {
  return (
    <span
      data-testid={testId}
      data-status={status}
      data-variant={loud ? 'loud' : 'quiet'}
      className={clsx(
        'inline-flex h-5 items-center gap-1.5 whitespace-nowrap text-xs leading-5',
        loud ? 'font-bold text-ink' : 'font-normal text-muted',
      )}
    >
      <span
        data-status-icon
        className={clsx(
          'inline-flex size-4 shrink-0 items-center justify-center rounded-[3px]',
          loud && block,
        )}
      >
        <Icon name={icon} size={loud ? 12 : 14} className={loud ? '[stroke-width:2.5]' : ''} />
      </span>
      {word}
    </span>
  )
}
