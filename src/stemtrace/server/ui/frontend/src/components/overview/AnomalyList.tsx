import type { ReactElement } from 'react'
import type { Anomaly } from '@/api/anomalies'
import { AnomalyFlag } from '@/components/anomalies/AnomalyFlag'
import { explain, sinceText } from '@/components/anomalies/anomalies'
import { TaskName } from '@/components/TaskName'
import { ALL_ANOMALIES_ID, anomalyEntryId } from '@/components/triage/anomalyId'
import { openerProps, usePanel } from '@/components/triage/PanelContext'
import type { OverviewAnomalies } from './anomalyData'

/** Items listed before "Show all". */
const SHOWN = 5

/**
 * The anomalies half of "What's wrong": the fleet item first, then the oldest items, each opening
 * in the triage panel. More than `SHOWN` end in a button that opens the full list.
 */
export function AnomalyList({ anomalies }: { anomalies: OverviewAnomalies }): ReactElement {
  const panel = usePanel()
  const rows: Anomaly[] = [...(anomalies.fleet ? [anomalies.fleet] : []), ...anomalies.items]
  return (
    <div className="min-w-0" data-testid="overview-anomalies">
      <h3 className="px-4 pt-2 pb-1.5 text-[13px] font-bold">Anomalies</h3>
      <ul className="m-0 list-none p-0">
        {rows.slice(0, SHOWN).map((anomaly) => (
          <li
            key={anomalyEntryId(anomaly)}
            data-testid="overview-anomaly"
            data-kind={anomaly.kind}
            className="relative flex gap-3 border-t border-rule px-4 py-3 first:border-t-0 hover:bg-hover has-[[data-on]]:bg-[color-mix(in_srgb,var(--ink)_10%,var(--surface))]"
          >
            <AnomalyFlag kind={anomaly.kind} className="mt-0.5 self-start" />
            <div className="min-w-0 flex-1">
              <button
                type="button"
                {...openerProps(panel, { kind: 'anomaly', id: anomalyEntryId(anomaly) })}
                className="block max-w-full text-left after:absolute after:inset-0"
              >
                {anomaly.name ? (
                  <TaskName name={anomaly.name} />
                ) : (
                  <span className="text-xs font-semibold">Fleet</span>
                )}
              </button>
              <p className="mt-0.5 mb-0 text-xs text-muted [overflow-wrap:anywhere]">
                {explain(anomaly)}
              </p>
              <p className="mt-0.5 mb-0 text-xs text-muted">since {sinceText(anomaly.since)}</p>
            </div>
          </li>
        ))}
      </ul>
      {rows.length > SHOWN && (
        <div className="px-4 pt-1 pb-2">
          <button
            type="button"
            {...openerProps(panel, { kind: 'anomaly', id: ALL_ANOMALIES_ID })}
            className="inline-flex h-[30px] items-center rounded-[3px] bg-raised px-3 text-xs font-semibold hover:bg-press"
          >
            Show all {rows.length}
          </button>
        </div>
      )}
    </div>
  )
}
