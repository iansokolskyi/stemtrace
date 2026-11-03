import type { ReactElement } from 'react'
import type { FailureGroup } from '@/api/client'
import { Icon } from '@/components/Icon'
import { TaskName } from '@/components/TaskName'
import { exceptionId } from '@/components/triage/exceptionId'
import { openerProps, usePanel } from '@/components/triage/PanelContext'
import { formatAgo, formatCount } from '@/utils/format'
import { AnomaliesWrongSlot } from './slots'

interface WhatsWrongProps {
  groups: FailureGroup[] | undefined
  /** "the last 6h"; none for All. */
  windowWords: string | undefined
}

const TH =
  'h-9 whitespace-nowrap border-b border-rule px-2.5 text-left text-xs font-semibold text-muted first:pl-4 last:pr-4'
const TD = 'px-2.5 py-1 text-xs first:pl-4 last:pr-4'

/**
 * Failures grouped by exception class, each row opening its group in the triage panel, with a
 * slot for the anomalies beside it. Always present: a window with no failures says so.
 */
export function WhatsWrong({ groups, windowWords }: WhatsWrongProps): ReactElement {
  const panel = usePanel()
  const scope = windowWords ? ` in ${windowWords}` : ''
  const count = groups?.length ?? 0
  return (
    <section aria-labelledby="h-wrong" className="mb-4 min-w-0 rounded-lg bg-surface">
      <div className="flex flex-wrap items-center gap-x-3.5 gap-y-1 px-4 pt-3.5 pb-2">
        <h2 id="h-wrong" className="text-sm font-bold">
          What’s wrong
        </h2>
        <span className="min-h-4 text-xs text-muted">
          {groups && `${count} failing exception${count === 1 ? '' : 's'}${scope}.`}
        </span>
      </div>
      <div className="pb-1.5 min-[1560px]:grid min-[1560px]:grid-cols-[minmax(0,1.7fr)_minmax(0,1fr)] min-[1560px]:gap-x-6">
        <div className="min-w-0">
          <h3 className="px-4 pt-2 pb-1.5 text-[13px] font-bold">Failures by exception</h3>
          {groups && count === 0 ? (
            <p className="m-0 flex items-center gap-2 px-4 py-3 text-xs text-muted">
              <Icon name="check" size={16} />
              All clear: no failures{scope}.
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
                  {(groups ?? []).map((group) => (
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
                          className="font-mono font-bold text-failure-t after:absolute after:inset-0"
                        >
                          {group.exception_type ?? 'No exception class'}
                        </button>
                      </td>
                      <td className={`${TD} max-w-[320px]`}>
                        <TaskName name={group.task_names[0] ?? ''} />
                        {group.task_names.length > 1 && (
                          <span className="text-muted">+{group.task_names.length - 1} more</span>
                        )}
                      </td>
                      <td className={`${TD} text-right font-bold tabular-nums`}>
                        {formatCount(group.count)}
                      </td>
                      <td className={`${TD} whitespace-nowrap text-muted`}>
                        {formatAgo(group.first_seen)}
                      </td>
                      <td className={`${TD} whitespace-nowrap text-muted`}>
                        {formatAgo(group.last_seen)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
        <AnomaliesWrongSlot />
      </div>
    </section>
  )
}
