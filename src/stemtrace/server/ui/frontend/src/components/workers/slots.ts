import type { Worker } from '@/api/client'

/** What a worker's pool fields let the UI say; each part is null when the server sent no value. */
export interface PoolUsage {
  /** The cell's line, for example `prefork pool, 3 of 8 slots active`; null with no pool info. */
  line: string | null
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
  const over = slots !== null && active !== null && active > slots
  const parts: string[] = []
  if (pool) parts.push(`${pool} pool`)
  if (slots !== null && active !== null) {
    parts.push(over ? `${active} active, ${slots} slots` : `${active} of ${slots} slots active`)
  } else if (slots !== null) {
    parts.push(`${slots} slots`)
  } else if (active !== null && pool) {
    parts.push(`${active} active`)
  }
  const both = slots !== null && active !== null
  return {
    line: pool || slots !== null ? parts.join(', ') : null,
    count: both
      ? over
        ? `${active} active, ${slots} slots`
        : `${active} of ${slots} slots`
      : null,
    fill: both ? Math.min(active, slots) / slots : null,
  }
}
