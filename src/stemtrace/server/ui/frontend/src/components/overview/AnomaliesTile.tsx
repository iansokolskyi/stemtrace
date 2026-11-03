import type { ReactElement } from 'react'
import type { AnomalyKind } from '@/api/anomalies'
import { ALL_ANOMALIES_ID } from '@/components/triage/anomalyId'
import { openerProps, usePanel } from '@/components/triage/PanelContext'
import type { OverviewAnomalies } from './anomalyData'
import { TileFace } from './TileFace'

const KIND_WORDS: Record<AnomalyKind, string> = {
  stuck: 'stuck',
  interrupted: 'interrupted',
  unregistered: 'unregistered',
  no_workers_online: 'no workers online',
}

/** The Anomalies tile: how many items, by kind; it opens the list in the triage panel. */
export function AnomaliesTile({
  anomalies,
  failed,
  className,
}: {
  anomalies: OverviewAnomalies | undefined
  failed: boolean
  className: string
}): ReactElement {
  const panel = usePanel()
  const total = anomalies?.total ?? 0
  const kinds = anomalies
    ? (Object.keys(KIND_WORDS) as AnomalyKind[])
        .filter((kind) => anomalies.counts[kind] > 0)
        .map((kind) => `${anomalies.counts[kind]} ${KIND_WORDS[kind]}`)
        .join(', ')
    : ''
  let note = ''
  if (failed) note = 'Unavailable'
  else if (anomalies) note = total > 0 ? kinds : 'None'
  return (
    <button
      type="button"
      {...openerProps(panel, { kind: 'anomaly', id: ALL_ANOMALIES_ID })}
      className={className}
    >
      <TileFace
        label="Anomalies"
        value={anomalies ? String(total) : ''}
        grade={total > 0 ? 'anomaly' : 'ok'}
        note={note}
        noteIcon="alert"
        sub="stuck, interrupted, unregistered"
      />
    </button>
  )
}
