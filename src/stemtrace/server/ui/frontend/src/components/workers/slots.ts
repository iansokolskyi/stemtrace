import type { Worker } from '@/api/client'

/** What a worker's pool fields let the UI say; each part is null when the server sent no value. */
export interface PoolUsage {
  /** The cell's line, for example `prefork pool, 3 of 8 slots active`; null with no pool info. */
  line: string | null
  /** True when a live `active` count backs the line: it then replaces the registered/seen line. */
  counted: boolean
  /** `3 of 8 slots`, for a row; null unless both numbers are known. */
  count: string | null
  /** Busy share of the slots, clamped to 0..1; null unless both numbers are known. */
  fill: number | null
}

const isCount = (value: number | null | undefined): value is number =>
  typeof value === 'number' && Number.isFinite(value) && value >= 0

/** Fields an older server doesn't send hide their part; nothing here is ever a placeholder. */
export function poolUsage(worker: Worker): PoolUsage {
  const pool = worker.pool || null
  const slots = isCount(worker.concurrency) && worker.concurrency > 0 ? worker.concurrency : null
  const active = isCount(worker.active) ? worker.active : null
  const known = slots !== null && active !== null
  const over = known && active > slots
  // One wording for the cell and the panel row; the cell adds " active" after the "of" form.
  const count = known
    ? over
      ? `${active} active, ${slots} slots`
      : `${active} of ${slots} slots`
    : null
  const parts: string[] = []
  if (pool) parts.push(`${pool} pool`)
  if (count !== null) parts.push(over ? count : `${count} active`)
  else if (slots !== null) parts.push(`${slots} slots`)
  else if (active !== null) parts.push(`${active} active`)
  const line = pool || slots !== null ? parts.join(', ') : null
  return {
    line,
    counted: line !== null && active !== null,
    count,
    fill: known ? Math.min(active, slots) / slots : null,
  }
}
