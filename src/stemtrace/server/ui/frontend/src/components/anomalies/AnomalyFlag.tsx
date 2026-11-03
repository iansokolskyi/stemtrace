import { clsx } from 'clsx'
import type { ReactElement } from 'react'
import type { AnomalyKind } from '@/api/anomalies'
import { Icon } from '@/components/Icon'
import { KIND_WORD } from './anomalies'

interface AnomalyFlagProps {
  kind: AnomalyKind
  className?: string
}

/** An anomaly kind as a filled anomaly icon block plus its word, never colour alone. */
export function AnomalyFlag({ kind, className }: AnomalyFlagProps): ReactElement {
  return (
    <span
      data-anomaly-flag={kind}
      className={clsx(
        'inline-flex h-5 items-center gap-1.5 whitespace-nowrap text-xs leading-5 font-bold text-ink',
        className,
      )}
    >
      <span className="inline-flex size-4 shrink-0 items-center justify-center rounded-[3px] bg-anomaly text-on-anomaly">
        <Icon name="alert" size={12} className="[stroke-width:2.5]" />
      </span>
      {KIND_WORD[kind]}
    </span>
  )
}
