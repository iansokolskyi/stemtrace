import { createFileRoute } from '@tanstack/react-router'
import type { ReactElement } from 'react'
import {
  AnomaliesTimeoutError,
  type Anomaly,
  anomalyTotal,
  useAnomalies,
  useFleetAnomaly,
} from '@/api/anomalies'
import { AnomalyActions } from '@/components/anomalies/AnomalyActions'
import { AnomalyCallout } from '@/components/anomalies/AnomalyCallout'
import { AnomalyFlag } from '@/components/anomalies/AnomalyFlag'
import { explain, sinceText } from '@/components/anomalies/anomalies'
import { Icon } from '@/components/Icon'
import { TaskName } from '@/components/TaskName'

export const Route = createFileRoute('/anomalies')({
  component: AnomaliesPage,
  staticData: { crumb: 'Anomalies' },
})

function AnomaliesPage(): ReactElement {
  const { data, isLoading, error } = useAnomalies()
  // The fleet item is one banner above the list, whatever its `since` sorts it to
  // The list is capped and sorts the fleet item last, so a full list may not hold it: `counts`
  // says whether there is one, and a request of its own fetches it.
  const inList = data?.anomalies.find((a) => a.kind === 'no_workers_online')
  const fleetQuery = useFleetAnomaly(Boolean(data?.counts.no_workers_online) && !inList)
  const fleet = inList ?? fleetQuery.data?.anomalies[0]
  const items = data?.anomalies.filter((a) => a.kind !== 'no_workers_online') ?? []
  const total = data ? anomalyTotal(data.counts) : 0

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-2xl font-bold text-ink">Anomalies</h1>
        <p className="mt-1 min-h-5 text-sm text-muted">
          {data && `${total} active: tasks that are stuck, interrupted or unregistered`}
        </p>
      </div>

      {error && (
        <p role="alert" data-testid="anomalies-error" className="text-failure-t">
          {error instanceof AnomaliesTimeoutError
            ? 'Anomalies are taking long to load'
            : 'Failed to load anomalies'}
          {data ? '; showing the last result.' : '.'}
        </p>
      )}
      {isLoading && <div aria-hidden="true" className="h-24 rounded-lg bg-surface" />}

      {data && total === 0 && (
        <section
          aria-label="Anomalies"
          data-testid="anomalies-empty"
          className="flex items-center gap-2 rounded-lg bg-surface px-4 py-3 text-muted"
        >
          <Icon name="check" size={16} className="flex-none" />
          All clear.
        </section>
      )}

      {fleet && <AnomalyCallout anomaly={fleet} placement="fleet" />}

      {data && data.total > data.anomalies.length && (
        <p data-testid="anomalies-truncated" className="text-xs text-muted">
          Showing {data.anomalies.length} of {data.total}
        </p>
      )}

      {items.length > 0 && (
        <section aria-label="Anomalies" className="rounded-lg bg-surface">
          <ul>
            {items.map((anomaly) => (
              <AnomalyItem key={itemKey(anomaly)} anomaly={anomaly} />
            ))}
          </ul>
        </section>
      )}
    </div>
  )
}

/** A task's anomaly is the task's; an unregistered item is its name's. */
function itemKey(anomaly: Anomaly): string {
  return `${anomaly.kind}:${anomaly.task_id ?? anomaly.name}`
}

function AnomalyItem({ anomaly }: { anomaly: Anomaly }): ReactElement {
  return (
    <li
      data-testid="anomaly-item"
      data-kind={anomaly.kind}
      className="flex min-w-0 gap-3 border-t border-rule px-4 py-3 first:border-t-0"
    >
      <AnomalyFlag kind={anomaly.kind} className="mt-0.5 self-start" />
      <div className="min-w-0 flex-1">
        {anomaly.name && <TaskName name={anomaly.name} className="[overflow-wrap:anywhere]" />}
        <p
          data-testid="anomaly-explanation"
          className="mt-0.5 mb-2 text-xs text-muted [overflow-wrap:anywhere]"
        >
          {explain(anomaly)}
        </p>
        <div className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1.5">
          <span className="text-xs text-muted">since {sinceText(anomaly.since)}</span>
          <AnomalyActions anomaly={anomaly} from="list" />
        </div>
      </div>
    </li>
  )
}
