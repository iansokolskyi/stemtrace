import type { ReactElement } from 'react'
import type { Anomaly } from '@/api/anomalies'
import { Icon } from '@/components/Icon'
import { AnomalyActions } from './AnomalyActions'
import { explain, KIND_HEADING, sinceText } from './anomalies'

interface AnomalyCalloutProps {
  anomaly: Anomaly
  /** `task`: the callout of a task page. `fleet`: the one banner above the Anomalies list. */
  placement: 'task' | 'fleet'
}

/** A tinted callout: what is wrong, since when, and the links onward. */
export function AnomalyCallout({ anomaly, placement }: AnomalyCalloutProps): ReactElement {
  const fleet = placement === 'fleet'
  return (
    <section
      data-testid={fleet ? 'fleet-banner' : 'anomaly-callout'}
      data-kind={anomaly.kind}
      aria-label={fleet ? 'Fleet anomaly' : 'Anomaly'}
      className="mb-4 flex gap-3 rounded-lg bg-[color-mix(in_srgb,var(--anomaly)_12%,var(--surface))] px-4 py-3.5"
    >
      <div className="flex size-7 flex-none items-center justify-center rounded-[3px] bg-anomaly text-on-anomaly">
        <Icon name="alert" size={18} className="[stroke-width:2.6]" />
      </div>
      <div className="min-w-0">
        <h2 className="mb-0.5 text-sm font-bold">{KIND_HEADING[anomaly.kind]}</h2>
        <p className="mb-2 text-[13px] [overflow-wrap:anywhere]">
          {explain(anomaly)}{' '}
          <span className="text-muted">
            {fleet ? 'Oldest' : 'Since'} {sinceText(anomaly.since)}.
          </span>
        </p>
        <AnomalyActions anomaly={anomaly} from={fleet ? 'list' : 'task'} />
      </div>
    </section>
  )
}
