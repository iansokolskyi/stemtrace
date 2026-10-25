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

/** A span as `120ms`, `1.5s`, `42s`, `3m 5s` or `1h 20m`; empty when unknown. */
export function formatSpan(ms: number | null | undefined): string {
  if (ms === null || ms === undefined || Number.isNaN(ms)) return ''
  if (ms < 1000) return `${Math.round(ms)}ms`
  if (ms < 10_000) return `${(ms / 1000).toFixed(1)}s`
  if (ms < 60_000) return `${Math.round(ms / 1000)}s`
  const totalSeconds = Math.round(ms / 1000)
  const minutes = Math.floor(totalSeconds / 60)
  const seconds = totalSeconds % 60
  if (minutes < 60) return seconds ? `${minutes}m ${seconds}s` : `${minutes}m`
  return `${Math.floor(minutes / 60)}h ${minutes % 60}m`
}

/** How long ago a timestamp was: `just now`, `5m ago`, `3h ago`, else the date; empty if none. */
export function formatAgo(timestamp: string | null): string {
  if (!timestamp) return ''
  const date = new Date(timestamp)
  const diff = Date.now() - date.getTime()
  if (diff < 60_000) return 'just now'
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)}m ago`
  if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)}h ago`
  return date.toLocaleDateString()
}

/** A URL date filter if it is a real YYYY-MM-DD date, else undefined. */
export function dateSearchParam(value: unknown): string | undefined {
  return typeof value === 'string' && parseDateParam(value) ? value : undefined
}
