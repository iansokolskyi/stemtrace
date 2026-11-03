import type { FetchExecutionsParams } from '@/api/client'

/**
 * Which runs a `runs` panel entry lists. It is the entry's id, so it reads the same wherever the
 * entry came from: a chart bar or a brush (a time span), a KPI tile (the window, maybe one
 * state), or one task name.
 */
export interface RunsScope {
  kind: 'bar' | 'brush' | 'kpi' | 'name'
  /** Start (inclusive) and end (exclusive) of a time span, as ISO times. */
  from?: string
  to?: string
  /** The Overview's window (`6h`) when the scope is the whole window; none for All. */
  within?: string
  /** A state or a comma-separated list. */
  state?: string
  name?: string
  /** What a tile called itself (the entry's title). */
  label?: string
}

const KEYS = ['kind', 'from', 'to', 'within', 'state', 'name', 'label'] as const

/** The entry id for a scope: query-string text with the keys in a fixed order. */
export function encodeScope(scope: RunsScope): string {
  const params = new URLSearchParams()
  for (const key of KEYS) {
    const value = scope[key]
    if (value) params.set(key, value)
  }
  return params.toString()
}

/** The scope an entry id names; undefined when it is not one (an old or hand-written link). */
export function decodeScope(id: string): RunsScope | undefined {
  const params = new URLSearchParams(id)
  const kind = params.get('kind')
  if (kind !== 'bar' && kind !== 'brush' && kind !== 'kpi' && kind !== 'name') return undefined
  const scope: RunsScope = { kind }
  for (const key of KEYS) {
    const value = params.get(key)
    if (key !== 'kind' && value) scope[key] = value
  }
  return scope
}

/** The list filters for a scope. A span is `from_date` to `updated_before`: the runs one bucket counts. */
export function scopeFilters(scope: RunsScope): FetchExecutionsParams {
  return {
    from_date: scope.from,
    updated_before: scope.to,
    within: scope.within,
    state: scope.state,
    name_exact: scope.name,
  }
}

/** A UTC clock time, `14:05`. */
export function clock(iso: string): string {
  return new Date(iso).toISOString().slice(11, 16)
}

/** `14:05–14:10` for a span; the entry's title and the brush's label. */
export function spanLabel(from: string, to: string): string {
  return `${clock(from)}–${clock(to)}`
}

/** What the panel calls the entry (its title and its crumb). */
export function scopeTitle(scope: RunsScope): string {
  if (scope.kind === 'name') return scope.name?.split('.').pop() ?? 'Runs'
  if (scope.label) return scope.label
  return scope.from && scope.to ? spanLabel(scope.from, scope.to) : 'Runs'
}
