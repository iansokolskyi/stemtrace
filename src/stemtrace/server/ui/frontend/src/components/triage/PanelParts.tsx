import { clsx } from 'clsx'
import type { ReactElement, ReactNode } from 'react'
import type { FailureListResponse, TaskStatsResponse } from '@/api/client'
import type { Bucket } from '@/components/overview/model'
import type { Host } from '@/components/workers/workers'
import type { Range } from '@/routes/-search'

/** What the Overview already loaded, which the panel's bodies read instead of asking again. */
export interface PanelData {
  buckets: Bucket[]
  failures: FailureListResponse | undefined
  stats: TaskStatsResponse | undefined
  hosts: Host[] | undefined
  range: Range
  /** The window the Overview's numbers cover (`6h`); none for All. */
  within: string | undefined
}

/** The Executions range that matches the Overview's window (`6h`; none is All). */
export const rangeOf = (within: string | undefined): Range =>
  within === '1h' || within === '6h' ? within : 'all'

export const BUTTON =
  'inline-flex h-[30px] max-w-full items-center gap-1.5 rounded-[3px] px-3 text-xs font-semibold whitespace-nowrap'
export const BUTTON_QUIET = `${BUTTON} bg-raised text-ink hover:bg-press`
export const BUTTON_PRIMARY = `${BUTTON} bg-ink text-on-ink hover:opacity-90`

/** A titled block of the panel body. */
export function Section({
  title,
  children,
}: {
  title?: string
  children: ReactNode
}): ReactElement {
  return (
    <div className="mt-4">
      {title && <h3 className="mb-1.5 text-[13px] font-bold">{title}</h3>}
      {children}
    </div>
  )
}

/** Label/value pairs; a pair without a value is left out. */
export function Facts({ rows }: { rows: [string, ReactNode][] }): ReactElement {
  return (
    <dl className="m-0 grid grid-cols-[96px_minmax(0,1fr)] gap-x-3 gap-y-1.5 text-xs">
      {rows
        .filter(
          ([, value]) => value !== null && value !== undefined && value !== '' && value !== false,
        )
        .map(([label, value]) => (
          <div key={label} className="contents">
            <dt className="text-muted">{label}</dt>
            <dd className="m-0 min-w-0 [overflow-wrap:anywhere]">{value}</dd>
          </div>
        ))}
    </dl>
  )
}

/** Four figures side by side (the panel's summary strip). */
export function Figures({
  items,
}: {
  items: { label: string; value: string; loud?: boolean }[]
}): ReactElement {
  return (
    <div className="grid grid-cols-4 gap-2">
      {items.map(({ label, value, loud }) => (
        <div key={label} className="min-w-0 rounded-[3px] bg-raised px-2.5 py-[7px]">
          <small className="block text-xs text-muted">{label}</small>
          <b className={clsx('text-[17px] font-extrabold', loud && 'text-failure-t')}>{value}</b>
        </div>
      ))}
    </div>
  )
}
