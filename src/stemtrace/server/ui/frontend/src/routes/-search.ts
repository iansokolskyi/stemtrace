/** Search params of the Executions page (the old task list's, unchanged). */
export interface ExecutionsSearch {
  state?: string
  name?: string
  name_exact?: string
  from_date?: string
  to_date?: string
  view?: 'list' | 'timeline'
}

const str = (v: unknown): string | undefined => (typeof v === 'string' ? v : undefined)

export function executionsSearch(search: Record<string, unknown>): ExecutionsSearch {
  return {
    state: str(search.state),
    name: str(search.name),
    name_exact: str(search.name_exact),
    from_date: str(search.from_date),
    to_date: str(search.to_date),
    view: search.view === 'list' || search.view === 'timeline' ? search.view : undefined,
  }
}

/** Search params of the Flows page (the old graphs list's, unchanged). */
export interface FlowsSearch {
  from_date?: string
  to_date?: string
}

export function flowsSearch(search: Record<string, unknown>): FlowsSearch {
  return { from_date: str(search.from_date), to_date: str(search.to_date) }
}
