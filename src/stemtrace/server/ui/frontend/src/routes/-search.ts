import type { ExecutionFilters } from '@/api/client'
import { dateSearchParam } from '@/utils/format'

/** The time range switch in the top strip. */
export type Range = '1h' | '6h' | 'all'

/** The range when the URL has none; it is never written to the URL. */
export const DEFAULT_RANGE: Range = '6h'

/** Search params of the Executions page: its filters (see `ExecutionFilters`), the view and the range. */
export interface ExecutionsSearch extends ExecutionFilters {
  view?: 'list' | 'timeline'
  range?: Range
}

// The router parses `?name=123` as the number 123: keep it as the text the user typed. Blank
// text is no filter.
const str = (v: unknown): string | undefined => {
  let text: string | undefined
  if (typeof v === 'string') text = v
  else if (typeof v === 'number') text = String(v)
  return text?.trim() ? text : undefined
}

/** The states a task can be in, in the order the filter lists them. */
export const TASK_STATES = [
  'PENDING',
  'RECEIVED',
  'STARTED',
  'SUCCESS',
  'FAILURE',
  'RETRY',
  'REVOKED',
  'REJECTED',
  'IGNORED',
] as const

/** A comma-separated state list in any case: known states only, upper-cased, each once. */
export function stateSearchParam(value: unknown): string | undefined {
  const text = str(value)
  if (!text) return undefined
  // Keep the order the URL gave: it is the order the user picked them in
  const kept: string[] = []
  for (const part of text.split(',')) {
    const state = part.trim().toUpperCase()
    if ((TASK_STATES as readonly string[]).includes(state) && !kept.includes(state)) {
      kept.push(state)
    }
  }
  return kept.length ? kept.join(',') : undefined
}

/**
 * `range` from a URL: `1h` or `all`. Absent, `6h` (the default) and anything else are no range.
 * The key is always present: the router keeps an unvalidated URL value for a missing key.
 */
export function rangeSearch(search: Record<string, unknown>): { range?: Range } {
  return { range: search.range === '1h' || search.range === 'all' ? search.range : undefined }
}

/** The `within` the API gets: the range's window, unless explicit dates set the window. */
export function withinOf(
  range: Range | undefined,
  { from_date, to_date }: ExecutionFilters,
): string | undefined {
  if (from_date || to_date) return undefined
  const effective = range ?? DEFAULT_RANGE
  return effective === 'all' ? undefined : effective
}

export function executionsSearch(search: Record<string, unknown>): ExecutionsSearch {
  return {
    state: stateSearchParam(search.state),
    name: str(search.name),
    name_exact: str(search.name_exact),
    exception_type: str(search.exception_type),
    hostname: str(search.hostname),
    queue: str(search.queue),
    task_group: str(search.task_group),
    tag: str(search.tag),
    from_date: dateSearchParam(search.from_date),
    to_date: dateSearchParam(search.to_date),
    view: search.view === 'list' || search.view === 'timeline' ? search.view : undefined,
    ...rangeSearch(search),
  }
}

/** Search params of the Flows page (the old graphs list's, unchanged). */
export interface FlowsSearch {
  from_date?: string
  to_date?: string
}

export function flowsSearch(search: Record<string, unknown>): FlowsSearch {
  return {
    from_date: dateSearchParam(search.from_date),
    to_date: dateSearchParam(search.to_date),
  }
}
