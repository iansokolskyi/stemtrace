import { Link } from '@tanstack/react-router'
import type { ReactElement } from 'react'
import { TaskName } from '@/components/TaskName'
import { formatCount, formatPercent, formatSpan } from '@/utils/format'
import { usePanel } from './PanelContext'
import { BUTTON_QUIET, Figures, type PanelData, Section } from './PanelParts'
import { encodeScope } from './runsScope'

const TH = 'h-8 border-b border-rule px-2.5 text-left text-xs font-semibold text-muted'

/** One task group over the window: its figures and its tasks, worst failure rate first. */
export function GroupBody({ id, data }: { id: string; data: PanelData }): ReactElement {
  const panel = usePanel()
  const group = data.stats?.groups?.find((g) => g.name === id)
  if (data.stats && !group)
    return <p className="m-0 pt-4 text-xs text-muted">This group has no runs in the window.</p>
  const tasks = (data.stats?.tasks ?? [])
    .filter((t) => t.task_group === id && t.runs > 0)
    .sort((a, b) => (b.failure_rate ?? 0) - (a.failure_rate ?? 0) || b.runs - a.runs)
  const rate = group?.failure_rate
  return (
    <>
      {group && (
        <Section>
          <Figures
            items={[
              { label: 'Runs', value: formatCount(group.runs) },
              {
                label: 'Fail %',
                value: rate == null ? '' : formatPercent(rate),
                loud: (rate ?? 0) > 0.01,
              },
              { label: 'p95', value: group.p95_ms == null ? '' : formatSpan(group.p95_ms) },
              { label: 'Tasks', value: String(tasks.length) },
            ]}
          />
        </Section>
      )}
      <Section title="Tasks">
        <div className="overflow-x-auto rounded-[3px]">
          <table aria-label="Tasks in the group" className="w-full border-collapse">
            <thead>
              <tr>
                <th scope="col" className={TH}>
                  Task
                </th>
                <th scope="col" className={`${TH} text-right`}>
                  Runs
                </th>
                <th scope="col" className={`${TH} text-right`}>
                  Fail %
                </th>
                <th scope="col" className={`${TH} text-right`}>
                  p95
                </th>
              </tr>
            </thead>
            <tbody>
              {tasks.map((task) => (
                <tr
                  key={task.name}
                  className="relative border-b border-rule last:border-b-0 hover:bg-hover"
                >
                  <td className="max-w-[260px] px-2.5 py-1.5">
                    <button
                      type="button"
                      onClick={() =>
                        panel.push({
                          kind: 'runs',
                          id: encodeScope({ kind: 'name', name: task.name, within: data.within }),
                        })
                      }
                      className="block max-w-full text-left after:absolute after:inset-0"
                    >
                      <TaskName name={task.name} />
                    </button>
                  </td>
                  <td className="px-2.5 py-1.5 text-right text-xs tabular-nums">
                    {formatCount(task.runs)}
                  </td>
                  <td
                    className={`px-2.5 py-1.5 text-right text-xs tabular-nums ${(task.failure_rate ?? 0) > 0.01 ? 'font-bold text-failure-t' : ''}`}
                  >
                    {task.failure_rate == null ? '' : formatPercent(task.failure_rate)}
                  </td>
                  <td className="px-2.5 py-1.5 text-right text-xs tabular-nums">
                    {task.p95_ms == null ? '' : formatSpan(task.p95_ms)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div className="mt-2.5">
          <Link
            to="/executions"
            search={{
              task_group: id,
              range: data.within === '1h' || data.within === '6h' ? data.within : 'all',
            }}
            className={BUTTON_QUIET}
          >
            Open in Executions
          </Link>
        </div>
      </Section>
    </>
  )
}
