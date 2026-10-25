/** How tightly the Executions rows are packed. */
export type Density = 'compact' | 'comfortable'

/** Row height in pixels per density (the virtualiser needs it up front). */
export const ROW_HEIGHT: Record<Density, number> = { compact: 32, comfortable: 44 }

export const DEFAULT_DENSITY: Density = 'comfortable'
