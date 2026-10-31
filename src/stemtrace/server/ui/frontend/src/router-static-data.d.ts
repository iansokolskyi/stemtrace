import '@tanstack/react-router'

declare module '@tanstack/react-router' {
  interface StaticDataRouteOption {
    /** The page's breadcrumb label. */
    crumb?: string
    /** The page's search has `range` (see `rangeSearch`): the top strip shows the time range switch. */
    timeRange?: true
    /** On a detail page, the list it belongs to: linked as the first breadcrumb. */
    section?: { label: string; to: '/executions' | '/flows' | '/workers' | '/registry' }
  }
}
