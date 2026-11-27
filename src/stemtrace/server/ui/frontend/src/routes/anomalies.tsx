import { createFileRoute } from '@tanstack/react-router'
import type { ReactElement } from 'react'
import { type Anomaly, anomalyTotal, useAnomalies, useFleetAnomaly } from '@/api/anomalies'
import { AnomalyActions } from '@/components/anomalies/AnomalyActions'
import { AnomalyCallout } from '@/components/anomalies/AnomalyCallout'
import { AnomalyFlag } from '@/components/anomalies/AnomalyFlag'
import { explain, sinceText } from '@/components/anomalies/anomalies'
import { Icon } from '@/components/Icon'
import { LoadFailure } from '@/components/Notice'
import { PageHeader } from '@/components/PageHeader'
import { PageStale } from '@/components/PageStale'
import { Skeleton, SkeletonRegion } from '@/components/Skeleton'
import { TaskName } from '@/components/TaskName'
import { useViewState } from '@/hooks/useViewState'

export const Route = createFileRoute('/anomalies')({
  component: AnomaliesPage,
  staticData: { crumb: 'Anomalies' },
})

/** Three list rows while the anomalies load. */
function AnomaliesSkeleton(): ReactElement {
  return (
    <SkeletonRegion label="anomalies" className="rounded-lg bg-surface">
      {[0, 1, 2].map((i) => (
        <div key={i} className="space-y-2 border-t border-rule px-4 py-3 first:border-t-0">
          <Skeleton className="h-4 w-56" />
          <Skeleton className="h-3 w-80 max-w-full" />
        </div>
      ))}
    </SkeletonRegion>
  )
}

function AnomaliesPage(): ReactElement {
  const query = useAnomalies()
  const { data } = query
  // The fleet item is one banner above the list, whatever its `since` sorts it to.
  // The list is capped and sorted by `since`, so a full list may not hold it: `counts` says
  // whether there is one, and a request of its own fetches it. That request does not ask the
  // workers again (`refresh=false`): the list request has just asked them.
  const inList = data?.anomalies.find((a) => a.kind === 'no_workers_online')
  const fleetAsked = Boolean(data?.counts.no_workers_online) && !inList
  const fleetQuery = useFleetAnomaly(fleetAsked)
  const view = useViewState(query, 'anomalies/list')
  const fleetView = useViewState(fleetQuery, 'anomalies/fleet')
  // A fleet query nobody asks for has nothing to refresh, so its old error must not show
  const views = fleetAsked ? [view, fleetView] : [view]
  // "All clear" and the counts say the answer is current: not while a refresh has failed
  const fresh = !views.some((v) => v.stale)
  const fleet = inList ?? fleetQuery.data?.anomalies[0]
  const items = data?.anomalies.filter((a) => a.kind !== 'no_workers_online') ?? []
  const total = data ? anomalyTotal(data.counts) : 0

  return (
    <div className="space-y-4">
      <PageHeader
        title="Anomalies"
        summary={
          data && fresh && `${total} active: tasks that are stuck, interrupted or unregistered`
        }
      />

      <PageStale views={views} className="" />
      {view.phase === 'error' && <LoadFailure what="anomalies" view={view} />}
      {view.phase === 'loading' && <AnomaliesSkeleton />}

      {data && fresh && total === 0 && (
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
      {fleetAsked && fleetView.phase === 'error' && !fleet && (
        <LoadFailure what="the notice that no workers are online" view={fleetView} />
      )}

      {data && data.total > data.anomalies.length && (
        <p data-testid="anomalies-truncated" className="text-small text-muted">
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
          className="mt-0.5 mb-2 text-small text-muted [overflow-wrap:anywhere]"
        >
          {explain(anomaly)}
        </p>
        <div className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1.5">
          <span className="text-small text-muted">since {sinceText(anomaly.since)}</span>
          <AnomalyActions anomaly={anomaly} from="list" />
        </div>
      </div>
    </li>
  )
}
