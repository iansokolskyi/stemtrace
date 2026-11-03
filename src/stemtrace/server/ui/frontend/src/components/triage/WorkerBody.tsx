import { Link } from '@tanstack/react-router'
import type { ReactElement } from 'react'
import { Icon } from '@/components/Icon'
import { WorkerStatus } from '@/components/workers/WorkerStatus'
import { registeredTasks } from '@/components/workers/workers'
import { formatAgo, formatDateTime } from '@/utils/format'
import { BUTTON_PRIMARY, BUTTON_QUIET, Facts, type PanelData, Section } from './PanelParts'
import { RunRows } from './RunRows'

/** One worker host: its status, what the server knows of it and its latest runs. */
export function WorkerBody({ id, data }: { id: string; data: PanelData }): ReactElement {
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
