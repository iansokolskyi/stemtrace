import type { Brush } from './RunsChart'

// Epoch seconds, at most 10 digits (year 2286): a longer number is not a time
const BRUSH = /^(\d{1,10})-(\d{1,10})$/

// The spelling the server uses for bucket instants (`…:00Z`), so a brush compares and opens as
// the same entry however it got here
const iso = (seconds: number): string =>
  new Date(seconds * 1000).toISOString().replace('.000Z', 'Z')

/** The brush from the `brush` URL value, `<from>-<to>` in epoch seconds; null when it is not a span. */
export function parseBrush(value: string | undefined): Brush | null {
  const match = value ? BRUSH.exec(value) : null
  if (!match) return null
  const [from, to] = [Number(match[1]), Number(match[2])]
  return from < to ? { from: iso(from), to: iso(to) } : null
}

/** The `brush` URL value for a brush, to the second; undefined for none (no param). */
export function formatBrush(brush: Brush | null): string | undefined {
  if (!brush) return undefined
  const from = Math.floor(Date.parse(brush.from) / 1000)
  const to = Math.floor(Date.parse(brush.to) / 1000)
  return from >= 0 && from < to ? `${from}-${to}` : undefined
}
