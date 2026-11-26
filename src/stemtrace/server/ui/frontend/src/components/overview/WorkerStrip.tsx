import { Link } from '@tanstack/react-router'
import { clsx } from 'clsx'
import type { ReactElement } from 'react'
import { openerProps, usePanel } from '@/components/triage/PanelContext'
import { poolUsage } from '@/components/workers/slots'
import { WorkerStatus } from '@/components/workers/WorkerStatus'
import { type Host, registeredTasks } from '@/components/workers/workers'
import { formatAgo, shortHost } from '@/utils/format'

/**
 * A cell per worker host: name, status (offline is the loud one) and what the server knows of
 * it, with its pool line and slot meter when the server sends them. A cell opens the worker in
 * the triage panel.
 */
export function WorkerStrip({ hosts }: { hosts: Host[] | undefined }): ReactElement {
  const panel = usePanel()
  return (
    <section aria-labelledby="h-workers" className="mb-4 min-w-0 rounded-lg bg-surface">
      <div className="flex flex-wrap items-center gap-x-3.5 gap-y-1 px-4 pt-3.5 pb-2">
        <h2 id="h-workers" className="text-sm font-bold">
          Workers
        </h2>
        <span className="grow" />
        <Link
          to="/workers"
          className="inline-flex h-[30px] items-center rounded-[3px] bg-raised px-3 text-xs font-semibold hover:bg-press"
        >
          All workers
        </Link>
      </div>
      {hosts && hosts.length === 0 ? (
        <p className="m-0 px-4 pt-2 pb-4 text-xs text-muted">No workers registered.</p>
      ) : (
        <div
          data-testid="worker-strip"
          className="grid grid-cols-2 gap-2.5 px-4 pt-2 pb-4 min-[900px]:grid-cols-3 min-[1280px]:grid-cols-5"
        >
          {(hosts ?? []).map(({ hostname, processes, leading }) => {
            const offline = leading.status === 'offline'
            const { line, fill } = poolUsage(leading)
            return (
              <button
                key={hostname}
                type="button"
                {...openerProps(panel, { kind: 'worker', id: hostname })}
                className={clsx(
                  'flex min-w-0 flex-col items-start gap-1 rounded-lg p-3 text-left hover:bg-press data-[on]:outline-2 data-[on]:-outline-offset-2 data-[on]:outline-ink',
                  offline ? 'bg-[color-mix(in_srgb,var(--failure)_8%,var(--raised))]' : 'bg-raised',
                )}
              >
                <span title={hostname} className="max-w-full truncate text-[13px] font-bold">
                  {shortHost(hostname)}
                </span>
                <WorkerStatus status={leading.status} />
                <span className={clsx('text-xs', offline ? 'text-ink' : 'text-muted')}>
                  {registeredTasks(processes).length} registered tasks, seen{' '}
                  {formatAgo(leading.last_seen)}
                </span>
                {line && (
                  <span data-testid="pool-line" className="text-xs text-muted">
                    {line}
                  </span>
                )}
                {fill !== null && (
                  <span
                    data-testid="slot-meter"
                    aria-hidden="true"
                    className="mt-0.5 block h-1.5 w-full overflow-hidden rounded-[2px] bg-ground"
                  >
                    <i className="block h-full bg-running" style={{ width: `${fill * 100}%` }} />
                  </span>
                )}
              </button>
            )
          })}
        </div>
      )}
    </section>
  )
}
