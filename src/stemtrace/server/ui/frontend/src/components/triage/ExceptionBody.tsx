import { Link } from '@tanstack/react-router'
import type { ReactElement } from 'react'
import { TaskName } from '@/components/TaskName'
import { fullFailureMessage } from '@/utils/failureMessage'
import { formatAgo, formatCount, formatDateTime } from '@/utils/format'
import { exceptionOfId } from './exceptionId'
import { usePanel } from './PanelContext'
import { BUTTON_QUIET, Facts, type PanelData, rangeOf, Section } from './PanelParts'
import { RunRows } from './RunRows'
import { encodeScope } from './runsScope'

/** One exception class: when it started and stopped, which tasks raise it, and its failing runs. */
export function ExceptionBody({ id, data }: { id: string; data: PanelData }): ReactElement {
  const panel = usePanel()
  const type = exceptionOfId(id)
  const group = data.failures?.groups.find((g) => g.exception_type === type)
  const message = fullFailureMessage(group)
  const stateOnly = { state: 'FAILURE', within: data.within }
  return (
    <>
      <Section>
        <Facts
          rows={[
            ['Message', message && <span className="font-mono">{message}</span>],
            [
              'Failures',
              group && <b className="font-mono text-failure-t">{formatCount(group.count)}</b>,
            ],
            [
              'First seen',
              group && `${formatDateTime(group.first_seen)} (${formatAgo(group.first_seen)})`,
            ],
            ['Last seen', group && formatAgo(group.last_seen)],
            [
              'Tasks',
              group && (
                <ul className="m-0 flex list-none flex-col gap-1 p-0">
                  {group.task_names.map((name) => (
                    <li key={name}>
                      <button
                        type="button"
                        onClick={() =>
                          panel.push({
                            kind: 'runs',
                            id: encodeScope({ kind: 'name', name, ...stateOnly }),
                          })
                        }
                        className="link max-w-full text-left"
                      >
                        <TaskName name={name} />
                      </button>
                    </li>
                  ))}
                </ul>
              ),
            ],
          ]}
        />
      </Section>
      <Section title="Failing runs">
        {type === null ? (
          <p className="m-0 text-small text-muted">
            These failures carry no exception class, so their runs cannot be listed here. Open a
            task above to see its runs.
          </p>
        ) : (
          <>
            <RunRows filters={{ ...stateOnly, exception_type: type }} />
            <div className="mt-2.5">
              <Link
                to="/executions"
                search={{
                  state: 'FAILURE',
                  exception_type: type,
                  range: rangeOf(data.within),
                }}
                className={BUTTON_QUIET}
              >
                Open in Executions
              </Link>
            </div>
          </>
        )}
      </Section>
    </>
  )
}
