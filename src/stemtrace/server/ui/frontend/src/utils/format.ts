export function formatDuration(ms: number | null): string {
  if (ms === null) return ''
  if (ms < 1000) return `${ms}ms`
  if (ms < 60000) return `${(ms / 1000).toFixed(1)}s`
  return `${(ms / 60000).toFixed(1)}m`
}

export function formatTime(timestamp: string | null): string {
  if (!timestamp) return ''
  const date = new Date(timestamp)
  return date.toLocaleTimeString('en-US', {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  })
}

/**
 * Format date as YYYY-MM-DD for clean URLs.
 */
export function formatDateParam(date: Date | undefined): string | undefined {
  if (!date) return undefined
  const year = date.getFullYear()
  const month = String(date.getMonth() + 1).padStart(2, '0')
  const day = String(date.getDate()).padStart(2, '0')
  return `${year}-${month}-${day}`
}

/**
 * Parse YYYY-MM-DD string to Date (start of day in local timezone).
 */
export function parseDateParam(str: string | undefined): Date | undefined {
  if (!str) return undefined
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(str)
  if (!match) return undefined
  // Parse as local date (not UTC); Date rolls 2026-02-30 over to March, so check it didn't
  const [year, month, day] = match.slice(1).map(Number)
  const date = new Date(year, month - 1, day)
  if (date.getFullYear() !== year || date.getMonth() !== month - 1 || date.getDate() !== day) {
    return undefined
  }
  return date
}

/** A URL date filter if it is a real YYYY-MM-DD date, else undefined. */
export function dateSearchParam(value: unknown): string | undefined {
  return typeof value === 'string' && parseDateParam(value) ? value : undefined
}
