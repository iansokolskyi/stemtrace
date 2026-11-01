import type { ReactElement } from 'react'
import { Icon, type IconName } from '@/components/Icon'
import { Tooltip } from '@/components/Tooltip'
import { useLiveStatus } from '@/hooks/useLiveStatus'

const TONE: Record<'ok' | 'warn' | 'bad', { icon: IconName; text: string }> = {
  ok: { icon: 'check', text: 'text-success-t' },
  warn: { icon: 'clock', text: 'text-retry-t' },
  bad: { icon: 'x', text: 'text-failure-t' },
}

/** How the UI gets updates, as a static word with an icon (no pulsing dot). */
export function LiveStatus(): ReactElement {
  const { label, tone, detail } = useLiveStatus()
  return (
    <Tooltip content={detail} placement="top-start">
      <span
        data-testid="live-status"
        className={`flex cursor-help items-center gap-1.5 text-xs font-bold ${TONE[tone].text}`}
      >
        <Icon name={TONE[tone].icon} size={12} />
        <b>{label}</b>{' '}
        {label === 'Live' && <span className="font-normal text-muted">streaming events</span>}
      </span>
    </Tooltip>
  )
}
