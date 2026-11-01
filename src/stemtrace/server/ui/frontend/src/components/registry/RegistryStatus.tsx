import { clsx } from 'clsx'
import type { ReactElement } from 'react'
import type { TaskStatus } from '@/api/client'
import { Icon } from '@/components/Icon'
import { STATUS_LOOK } from './registry'

/**
 * A task's registry status as icon plus word, never colour alone. Active and never run are
 * quiet (muted regular text, outline icon); not registered is the one loud thing on the page
 * (filled anomaly block, bold ink).
 */
export function RegistryStatus({ status }: { status: TaskStatus }): ReactElement {
  const look = STATUS_LOOK[status]
  return (
    <span
      data-testid="registry-status"
      data-status={status}
      data-variant={look.loud ? 'loud' : 'quiet'}
      className={clsx(
        'inline-flex h-5 items-center gap-1.5 whitespace-nowrap text-xs leading-5',
        look.loud ? 'font-bold text-ink' : 'font-normal text-muted',
      )}
    >
      <span
        data-status-icon
        className={clsx(
          'inline-flex size-4 shrink-0 items-center justify-center rounded-[3px]',
          look.loud && 'bg-anomaly text-on-anomaly',
        )}
      >
        <Icon
          name={look.icon}
          size={look.loud ? 12 : 14}
          className={look.loud ? '[stroke-width:2.5]' : ''}
        />
      </span>
      {look.word}
    </span>
  )
}
