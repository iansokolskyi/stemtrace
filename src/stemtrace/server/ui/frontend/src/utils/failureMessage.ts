import type { FailureGroup } from '@/api/client'

/**
 * The message to show under an exception name, or undefined for none. The worker writes
 * `"<ShortName>: <msg>"`, so one leading `"<last segment of the class>: "` is dropped.
 */
export function failureLine(group: FailureGroup): string | undefined {
  const text = group.latest_message?.trim()
  if (!text) return undefined
  const name = group.exception_type?.split('.').pop()
  // `trim` also removed the space after the colon of a bare "<ShortName>: ".
  const stripped = name && `${text} `.startsWith(`${name}: `) ? text.slice(name.length + 1) : text
  const message = stripped.trim()
  return message || undefined
}

/** The group's message as stored (trimmed), or undefined when there is none. */
export function fullFailureMessage(group: FailureGroup | undefined): string | undefined {
  return group?.latest_message?.trim() || undefined
}
