import '@tanstack/react-router'

declare module '@tanstack/react-router' {
  interface StaticDataRouteOption {
    /** The page's breadcrumb label. */
    crumb?: string
    /** On a detail page, the list it belongs to: linked as the first breadcrumb. */
    section?: { label: string; to: '/executions' | '/flows' | '/workers' | '/registry' }
  }
}
