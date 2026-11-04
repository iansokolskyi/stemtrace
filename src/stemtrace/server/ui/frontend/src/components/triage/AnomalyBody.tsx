import type { ReactElement } from 'react'
import type { Anomaly } from '@/api/anomalies'
import { AnomalyActions } from '@/components/anomalies/AnomalyActions'
import { AnomalyFlag } from '@/components/anomalies/AnomalyFlag'
import { explain, kindHeading, sinceText } from '@/components/anomalies/anomalies'
import { Icon } from '@/components/Icon'
import { TaskName } from '@/components/TaskName'
import { ALL_ANOMALIES_ID, anomalyEntryId } from './anomalyId'
import { usePanel } from './PanelContext'
import type { PanelData } from './PanelParts'
import { Section } from './PanelParts'
import { RunRows } from './RunRows'
import { TaskBody } from './TaskBody'

/** The anomaly an `anomaly` entry id names, fleet item included. */
export function findAnomaly(data: PanelData, id: string): Anomaly | undefined {
  const all = [
    ...(data.anomalies?.fleet ? [data.anomalies.fleet] : []),
    ...(data.anomalies?.items ?? []),
  ]
  return all.find((a) => anomalyEntryId(a) === id)
}

/**
 * An `anomaly` entry: `all` lists every item (each row opens one), anything else is one item
 * with why it is flagged and its links, then its task, or for an unregistered name its runs.
 */
export function AnomalyBody({ id, data }: { id: string; data: PanelData }): ReactElement {
  const panel = usePanel()
  if (!data.anomalies) return <div className="h-24" aria-hidden="true" />

  if (id === ALL_ANOMALIES_ID) {
    const rows = [...(data.anomalies.fleet ? [data.anomalies.fleet] : []), ...data.anomalies.items]
    if (rows.length === 0) {
      return (
        <p className="m-0 flex items-center gap-2 pt-4 text-xs text-muted">
          <Icon name="check" size={16} />
          All clear.
        </p>
      )
    }
    return (
      <Section>
        <ul className="m-0 list-none overflow-hidden rounded-[3px] p-0">
          {rows.map((anomaly) => (
            <li
              key={anomalyEntryId(anomaly)}
              className="relative flex gap-3 border-t border-rule px-2.5 py-2.5 first:border-t-0 hover:bg-hover"
            >
              <AnomalyFlag kind={anomaly.kind} className="mt-0.5 self-start" />
              <div className="min-w-0 flex-1">
                <button
                  type="button"
                  onClick={() => panel.push({ kind: 'anomaly', id: anomalyEntryId(anomaly) })}
                  className="block max-w-full text-left after:absolute after:inset-0"
                >
                  {anomaly.name ? (
                    <TaskName name={anomaly.name} />
                  ) : (
                    <span className="text-xs font-semibold">Fleet</span>
                  )}
                </button>
                <p className="m-0 text-xs text-muted [overflow-wrap:anywhere]">
                  {explain(anomaly)}
                </p>
              </div>
            </li>
          ))}
        </ul>
      </Section>
    )
  }

  const anomaly = findAnomaly(data, id)
  if (!anomaly) return <p className="m-0 pt-4 text-xs text-muted">This anomaly has cleared.</p>
  return (
    <>
      <Section>
        <div
          data-testid="panel-anomaly"
          data-kind={anomaly.kind}
          className="flex min-w-0 gap-3 rounded-lg bg-[color-mix(in_srgb,var(--anomaly)_12%,var(--surface))] px-3.5 py-3"
        >
          <div className="flex size-7 flex-none items-center justify-center rounded-[3px] bg-anomaly text-on-anomaly">
            <Icon name="alert" size={18} className="[stroke-width:2.6]" />
          </div>
          <div className="min-w-0">
            <h3 className="m-0 mb-0.5 text-sm font-bold">{kindHeading(anomaly)}</h3>
            <p className="m-0 mb-2 text-[13px] [overflow-wrap:anywhere]">
              {explain(anomaly)}{' '}
              <span className="text-muted">Since {sinceText(anomaly.since)}.</span>
            </p>
            <AnomalyActions anomaly={anomaly} from="list" />
          </div>
        </div>
      </Section>
      {anomaly.task_id ? (
        <TaskBody id={anomaly.task_id} />
      ) : (
        anomaly.name && (
          <Section title="Runs">
            <RunRows filters={{ name_exact: anomaly.name }} initial={10} />
          </Section>
        )
      )}
    </>
  )
}
