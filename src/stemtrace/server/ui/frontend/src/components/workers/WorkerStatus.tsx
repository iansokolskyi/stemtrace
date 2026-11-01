import { clsx } from 'clsx'
import type { ReactElement } from 'react'
import type { Worker } from '@/api/client'
import { Icon } from '@/components/Icon'

interface WorkerStatusProps {
  status: Worker['status']
}

/**
 * A worker's status as icon plus word, never colour alone. Online is quiet (muted regular
 * text, outline icon); offline is the one loud thing on the page (filled failure block, bold ink).
 */
export function WorkerStatus({ status }: WorkerStatusProps): ReactElement {
  const offline = status === 'offline'
  return (
    <span
      data-testid="worker-status"
      data-status={status}
      data-variant={offline ? 'loud' : 'quiet'}
      className={clsx(
        'inline-flex h-5 items-center gap-1.5 whitespace-nowrap text-xs leading-5',
        offline ? 'font-bold text-ink' : 'font-normal text-muted',
      )}
    >
      <span
        data-status-icon
        className={clsx(
          'inline-flex size-4 shrink-0 items-center justify-center rounded-[3px]',
          offline && 'bg-failure text-on-failure',
        )}
      >
        <Icon
          name={offline ? 'x' : 'check'}
          size={offline ? 12 : 14}
          className={offline ? '[stroke-width:2.5]' : ''}
        />
      </span>
      {offline ? 'Offline' : 'Online'}
    </span>
  )
}
