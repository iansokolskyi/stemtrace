import type { TimeseriesResponse } from '@/api/client'

/** The states a run's outcome is (what the overview's rates divide by); mirrors `OUTCOME_STATES` in `server/stats.py`. */
const OUTCOME_STATES = ['SUCCESS', 'FAILURE', 'REVOKED', 'REJECTED'] as const

/** One bucket of the Runs chart. */
export interface Bucket {
  /** ISO start (inclusive) and end (exclusive). */
  start: string
  end: string
  success: number
  failure: number
  retry: number
  /** Runs with an outcome: the base of the rates and of the sparklines. */
  outcomes: number
}

/** The buckets of a time series, in order, with each one's counts by what the chart draws. */
export function bucketsOf(series: TimeseriesResponse): Bucket[] {
  const count = (state: string, i: number): number => series.counts[state]?.[i] ?? 0
  return series.starts.map((start, i) => {
    const next = series.starts[i + 1]
    // The server writes `…:00Z`; the last end is built in that form too
    const end =
      next ??
      new Date(Date.parse(start) + series.bucket_seconds * 1000).toISOString().replace('.000Z', 'Z')
    return {
      start,
      end,
      success: count('SUCCESS', i),
      failure: count('FAILURE', i),
      retry: count('RETRY', i),
      outcomes: OUTCOME_STATES.reduce((sum, state) => sum + count(state, i), 0),
    }
  })
}

/** How far a KPI is from the window before it. */
export type Grade = 'ok' | 'warn' | 'bad'

/**
 * `ok` within the `warn` ratio of the baseline, `warn` from it, `bad` from the `bad` ratio. With
 * `both`, a fall counts as much as a rise (a runs rate that halves is as odd as one that doubles).
 * A zero baseline has nothing to compare with: `ok`.
 */
export function gradeOf(
  value: number,
  baseline: number,
  warn: number,
  bad: number,
  both = false,
): Grade {
  // A rise from nothing is worth a look (failures that were absent); a fall from nothing is not
  if (baseline <= 0) return value > 0 && !both ? 'warn' : 'ok'
  const ratio = value / baseline
  const factor = both && ratio < 1 ? (ratio > 0 ? 1 / ratio : Number.POSITIVE_INFINITY) : ratio
  if (factor >= bad) return 'bad'
  return factor >= warn ? 'warn' : 'ok'
}

/** The tile's second line: `In baseline`, or how far above or below it the value is. */
export function baselineNote(grade: Grade, value: number, baseline: number): string {
  if (grade === 'ok') return baseline > 0 ? 'In baseline' : ''
  if (baseline <= 0) return 'Above baseline'
  const ratio = value / baseline
  const far = ratio >= 2 || ratio <= 0.5
  const amount = far
    ? `${ratio.toFixed(1)}×`
    : `${ratio > 1 ? '+' : '−'}${Math.abs((ratio - 1) * 100).toFixed(0)}%`
  return `${grade === 'bad' ? 'Well ' : ''}${ratio > 1 ? 'above' : 'below'} baseline: ${amount}`
}

/** Runs per minute, `2.4`; `0` stays `0`. */
export const formatRate = (perMinute: number): string =>
  perMinute === 0 ? '0' : perMinute < 10 ? perMinute.toFixed(1) : String(Math.round(perMinute))

/** Whether a span ending at `to` ends on the last bucket, which the server leaves open-ended (`stats.py` `count_runs`). */
export const endsOpen = (buckets: Bucket[], to: string): boolean => {
  const last = buckets[buckets.length - 1]
  // Instants, not strings: the server writes `…:00Z` and a client-built link `…:00.000Z`
  return last !== undefined && Date.parse(to) >= Date.parse(last.end)
}

/** Whether two ISO times are the same instant, whatever their spelling. */
export const sameInstant = (a: string, b: string): boolean => Date.parse(a) === Date.parse(b)
