import type { ExecutionRow } from '@/api/client'
import { formatAgo, formatSpan } from '@/utils/format'

export type ColumnId =
  | 'state'
  | 'name'
  | 'group'
  | 'worker'
  | 'queue'
  | 'queue_wait'
  | 'runtime'
  | 'updated'

export interface Column {
  id: ColumnId
  label: string
  /** CSS grid track of the column. */
  track: string
  /** Narrowest width in pixels, so the table box scrolls sideways instead of squeezing. */
  min: number
  align?: 'right'
  /** Whether a row has something to show; an optional column needs it in at least one row. */
  has?: (row: ExecutionRow) => boolean
  /** The cell text (the state and name cells render components instead). */
  text?: (row: ExecutionRow) => string
}

const present = (value: string | null | undefined): boolean => !!value
const finite = (value: number | null | undefined): boolean => value !== null && value !== undefined

/** Strips the `celery@` every default worker name starts with. */
export const shortHost = (hostname: string): string => hostname.replace(/^celery@/, '')

/** Every column in display order. State, name and updated always show; the rest are optional. */
export const COLUMNS: Column[] = [
  { id: 'state', label: 'State', track: '104px', min: 104 },
  { id: 'name', label: 'Task', track: 'minmax(260px, 1fr)', min: 260 },
  {
    id: 'group',
    label: 'Group',
    track: '112px',
    min: 112,
    has: (row) => present(row.task_group),
    text: (row) => row.task_group ?? '',
  },
  {
    id: 'worker',
    label: 'Worker',
    track: '144px',
    min: 144,
    has: (row) => present(row.hostname),
    text: (row) => (row.hostname ? shortHost(row.hostname) : ''),
  },
  {
    id: 'queue',
    label: 'Queue',
    track: '92px',
    min: 92,
    has: (row) => present(row.queue),
    text: (row) => row.queue ?? '',
  },
  {
    id: 'queue_wait',
    label: 'Queue wait',
    track: '88px',
    min: 88,
    align: 'right',
    has: (row) => finite(row.queue_wait_ms),
    text: (row) => formatSpan(row.queue_wait_ms),
  },
  {
    id: 'runtime',
    label: 'Runtime',
    track: '80px',
    min: 80,
    align: 'right',
    has: (row) => finite(row.runtime_ms),
    text: (row) => formatSpan(row.runtime_ms),
  },
  {
    id: 'updated',
    label: 'Updated',
    track: '88px',
    min: 88,
    align: 'right',
    text: (row) => formatAgo(row.last_updated),
  },
]

/**
 * The columns to show for `firstPage`: the always-on ones, and each optional one that at
 * least one row has a value for (the API sends every field, null when unknown, so absent means
 * null in every row). Called once per page load, never again while scrolling or refetching.
 */
export function columnsFor(firstPage: ExecutionRow[]): Column[] {
  return COLUMNS.filter((column) => !column.has || firstPage.some(column.has))
}
