import type { ReactElement } from 'react'
import type { FailureGroup } from '@/api/client'
import { Icon } from '@/components/Icon'
import { TaskName } from '@/components/TaskName'
import { exceptionId } from '@/components/triage/exceptionId'
import { openerProps, usePanel } from '@/components/triage/PanelContext'
import type { ViewState } from '@/hooks/useViewState'
import { failureLine } from '@/utils/failureMessage'
import { formatAgo, formatCount } from '@/utils/format'
import { AnomalyList } from './AnomalyList'
import type { OverviewAnomalies } from './anomalyData'
import { LoadError } from './LoadError'

interface WhatsWrongProps {
  groups: FailureGroup[] | undefined
  /** The failures request's state; an error or loading shows instead of the table. */
  view: ViewState
  /** A refresh failed with earlier data held: neither the counts nor "All clear" are current. */
  stale: boolean
  /** "the last 6h"; none for All. */
  windowWords: string | undefined
  /** Undefined while loading and when the server has no anomalies endpoint. */
  anomalies: OverviewAnomalies | undefined
}

const TH = 'th-band h-9 whitespace-nowrap px-2.5 text-left first:pl-4 last:pr-4'
const TD = 'px-2.5 py-1 text-small first:pl-4 last:pr-4'

/**
 * Failures grouped by exception class and the anomalies now, each row opening in the triage
 * panel. Always present: with neither it says "All clear" (only once the anomalies are known).
 */
export function WhatsWrong({
  groups,
  view,
  stale,
  windowWords,
  anomalies,
}: WhatsWrongProps): ReactElement {
  const panel = usePanel()
  const scope = windowWords ? ` in ${windowWords}` : ''
  const count = groups?.length ?? 0
  const failuresNone = groups !== undefined && count === 0
  const hasAnomalies = (anomalies?.total ?? 0) > 0
  const allClear = failuresNone && !stale && anomalies !== undefined && !hasAnomalies
  return (
    <section aria-labelledby="h-wrong" className="mb-4 min-w-0 rounded-lg bg-surface">
      <div className="flex flex-wrap items-center gap-x-3.5 gap-y-1 px-4 pt-3.5 pb-2">
        <h2 id="h-wrong" className="text-title font-semibold">
          What’s wrong
        </h2>
        <span className="min-h-4 text-small text-muted">
          {groups &&
            !stale &&
            `${count} failing exception${count === 1 ? '' : 's'}${scope}.${
              anomalies
                ? ` ${anomalies.total} anomal${anomalies.total === 1 ? 'y' : 'ies'} now.`
                : ''
            }`}
        </span>
      </div>
      {allClear ? (
        <p className="m-0 flex items-center gap-2 px-4 pt-2 pb-4 text-small text-muted">
          <Icon name="check" size={16} />
          All clear: no failures or anomalies{scope}.
        </p>
      ) : (
        <div
          className={
            hasAnomalies
              ? 'pb-1.5 min-[1560px]:grid min-[1560px]:grid-cols-[minmax(0,1.7fr)_minmax(0,1fr)] min-[1560px]:gap-x-6'
              : 'pb-1.5'
          }
        >
          <div className="min-w-0">
            <h3 className="px-4 pt-2 pb-1.5 text-small font-medium text-muted">
              Failures by exception
            </h3>
            {view.phase === 'error' ? (
              <LoadError what="the failures" view={view} />
            ) : groups === undefined ? (
              <p className="m-0 px-4 py-3 text-small text-muted">Loading failures…</p>
            ) : failuresNone ? (
              <p className="m-0 flex items-center gap-2 px-4 py-3 text-small text-muted">
                <Icon name="check" size={16} />
                No failures{scope}.
              </p>
            ) : (
              <div className="overflow-x-auto">
                <table aria-label="Failures by exception" className="w-full border-collapse">
                  <thead>
                    <tr>
                      <th scope="col" className={TH}>
                        Exception
                      </th>
                      <th scope="col" className={TH}>
                        Task
                      </th>
                      <th scope="col" className={`${TH} text-right`}>
                        Count
                      </th>
                      <th scope="col" className={TH}>
                        First seen
                      </th>
                      <th scope="col" className={TH}>
                        Last seen
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    {(groups ?? []).map((group) => {
                      const message = failureLine(group)
                      return (
                        <tr
                          key={group.exception_type ?? ''}
                          className="relative border-b border-rule last:border-b-0 hover:bg-hover has-[[data-on]]:bg-[color-mix(in_srgb,var(--ink)_10%,var(--surface))]"
                        >
                          <td className={TD}>
                            <button
                              type="button"
                              {...openerProps(panel, {
                                kind: 'exception',
                                id: exceptionId(group.exception_type),
                              })}
                              // The overlay covers the message line, so the hover text lives here.
                              title={message}
                              className="font-medium text-ink after:absolute after:inset-0"
                            >
                              {group.exception_type ?? 'No exception class'}
                            </button>
                            {message && (
                              <div
                                data-testid="failure-message"
                                className="max-w-[300px] truncate text-meta text-muted"
                              >
                                {message}
                              </div>
                            )}
                          </td>
                          <td className={`${TD} max-w-[320px]`}>
                            <TaskName name={group.task_names[0] ?? ''} />
                            {group.task_names.length > 1 && (
                              <span className="text-muted">
                                +{group.task_names.length - 1} more
                              </span>
                            )}
                          </td>
                          <td className={`${TD} text-right font-medium tabular-nums`}>
                            {formatCount(group.count)}
                          </td>
                          <td className={`${TD} whitespace-nowrap text-muted`}>
                            {formatAgo(group.first_seen)}
                          </td>
                          <td className={`${TD} whitespace-nowrap text-muted`}>
                            {formatAgo(group.last_seen)}
                          </td>
                        </tr>
                      )
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </div>
          {anomalies && hasAnomalies && <AnomalyList anomalies={anomalies} />}
        </div>
      )}
    </section>
  )
}
