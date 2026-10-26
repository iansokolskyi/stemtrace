import type { TaskGroup, TaskNode } from '@/api/client'

/** The fields of a task row that suggestions read; a list row and a timeline node both have them. */
type SuggestionRow = Pick<TaskNode, 'hostname' | 'queue' | 'exception_type' | 'task_group' | 'tags'>

/** Values the filter popovers offer, from data the page has already loaded. */
export interface FilterSuggestions {
  hostname: string[]
  queue: string[]
  exception_type: string[]
  task_group: string[]
  tag: string[]
}

const distinct = (values: (string | null | undefined)[]): string[] =>
  [...new Set(values.filter((v): v is string => !!v))].sort((a, b) => a.localeCompare(b))

/**
 * The distinct workers, queues, exceptions, groups and tags in the loaded rows, plus the groups
 * and tags `/api/groups` lists (a group with no runs yet is still a valid filter). No request
 * of their own: a value the loaded rows don't have can still be typed.
 */
export function suggestionsFrom(
  rows: SuggestionRow[],
  groups: TaskGroup[] | undefined,
): FilterSuggestions {
  return {
    hostname: distinct(rows.map((row) => row.hostname)),
    queue: distinct(rows.map((row) => row.queue)),
    exception_type: distinct(rows.map((row) => row.exception_type)),
    task_group: distinct([
      ...rows.map((row) => row.task_group),
      ...(groups ?? []).map((g) => g.name),
    ]),
    tag: distinct([
      ...rows.flatMap((row) => row.tags ?? []),
      ...(groups ?? []).flatMap((g) => g.tags),
    ]),
  }
}
