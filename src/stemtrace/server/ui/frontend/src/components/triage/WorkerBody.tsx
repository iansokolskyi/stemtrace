import { Link } from '@tanstack/react-router'
import type { ReactElement } from 'react'
import { Icon } from '@/components/Icon'
import { WorkerStatus } from '@/components/workers/WorkerStatus'
import { type Host, registeredTasks } from '@/components/workers/workers'
import { formatAgo, formatDateTime, shortHost } from '@/utils/format'
import { usePanel } from './PanelContext'
import { BUTTON_PRIMARY, BUTTON_QUIET, Facts, type PanelData, Section } from './PanelParts'
import { RunRows } from './RunRows'
import { ALL_WORKERS_ID } from './workerId'

/** Offline hosts first, then by name. */
const byTriage = (hosts: Host[]): Host[] =>
  [...hosts].sort(
    (a, b) =>
      Number(b.leading.status === 'offline') - Number(a.leading.status === 'offline') ||
      a.hostname.localeCompare(b.hostname),
  )

/** The `all` entry: a row per host, each opening that host, and the link to the Workers page. */
function AllWorkers({ hosts }: { hosts: Host[] | undefined }): ReactElement {
  const panel = usePanel()
  if (!hosts) return <div className="h-24" aria-hidden="true" />
  return (
    <>
      <Section>
        {hosts.length === 0 ? (
          <p className="m-0 text-xs text-muted">No workers registered.</p>
        ) : (
          <ul className="m-0 list-none overflow-hidden rounded-[3px] p-0">
            {byTriage(hosts).map(({ hostname, leading }) => (
              <li
                key={hostname}
                className="relative flex items-center gap-3 border-t border-rule px-2.5 py-2.5 first:border-t-0 hover:bg-hover"
              >
                <button
                  type="button"
                  title={hostname}
                  onClick={() => panel.push({ kind: 'worker', id: hostname })}
                  className="min-w-0 flex-1 truncate text-left text-[13px] font-bold after:absolute after:inset-0"
                >
                  {shortHost(hostname)}
                </button>
                <WorkerStatus status={leading.status} />
              </li>
            ))}
          </ul>
        )}
      </Section>
      <Section>
        <Link to="/workers" className={BUTTON_QUIET}>
          All workers
        </Link>
      </Section>
    </>
  )
}

/** One worker host: its status, what the server knows of it and its latest runs. */
export function WorkerBody({ id, data }: { id: string; data: PanelData }): ReactElement {
  if (id === ALL_WORKERS_ID) return <AllWorkers hosts={data.hosts} />
  const host = data.hosts?.find((h) => h.hostname === id)
  if (data.hosts && !host)
    return <p className="m-0 pt-4 text-xs text-muted">This worker is not known to the server.</p>
  const worker = host?.leading
  return (
    <>
      <Section>{worker && <WorkerStatus status={worker.status} />}</Section>
      {worker?.status === 'offline' && (
        <Section>
          <div className="flex gap-3 rounded-lg bg-[color-mix(in_srgb,var(--failure)_12%,var(--surface))] p-3.5">
            <span className="inline-flex size-7 flex-none items-center justify-center rounded-[3px] bg-failure text-on-failure">
              <Icon name="x" size={16} className="[stroke-width:2.6]" />
            </span>
            <div>
              <h4 className="m-0 text-sm font-bold">Offline since {formatAgo(worker.last_seen)}</h4>
              <p className="m-0 text-[13px]">
                Last seen {formatDateTime(worker.last_seen)}. Tasks that were running on it can show
                as interrupted.
              </p>
            </div>
          </div>
        </Section>
      )}
      <Section>
        <Facts
          rows={[
            ['Registered', host && `${registeredTasks(host.processes).length} tasks`],
            ['Started', worker && formatAgo(worker.registered_at)],
            ['Last seen', worker && formatAgo(worker.last_seen)],
          ]}
        />
        <div className="mt-2.5 flex flex-wrap gap-1.5">
          <Link to="/workers/$hostname" params={{ hostname: id }} className={BUTTON_PRIMARY}>
            Open worker page
          </Link>
          <Link to="/executions" search={{ hostname: id }} className={BUTTON_QUIET}>
            Open in Executions
          </Link>
        </div>
      </Section>
      <Section title="Recent runs">
        <RunRows filters={{ hostname: id }} initial={10} />
      </Section>
    </>
  )
}
