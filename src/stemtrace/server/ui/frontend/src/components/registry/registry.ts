import type { RegisteredTask, TaskStatus } from '@/api/client'
import type { IconName } from '@/components/Icon'

/** What each status is called and drawn as. Only `not_registered` is loud: it is the problem. */
export const STATUS_LOOK: Record<TaskStatus, { word: string; icon: IconName; loud: boolean }> = {
  active: { word: 'Active', icon: 'check', loud: false },
  never_run: { word: 'Never run', icon: 'clock', loud: false },
  not_registered: { word: 'Not registered', icon: 'alert', loud: true },
}

/** How many worker names a never-run note lists before it counts the rest. */
const MAX_HOSTS_IN_NOTE = 2

/** The sentence under a name that explains its status; none for an active task. */
export function noteOf(task: RegisteredTask): string | null {
  if (task.status === 'not_registered') {
    return 'No worker has this task registered. New runs with this name may stay in Pending.'
  }
  if (task.status === 'never_run') {
    // The server only calls a name never_run when a worker registers it, so there is a host
    const hosts = task.registered_by.slice(0, MAX_HOSTS_IN_NOTE).join(', ')
    const more = task.registered_by.length - MAX_HOSTS_IN_NOTE
    return `Registered by ${hosts}${more > 0 ? ` and ${more} more` : ''}; it has not run yet.`
  }
  return null
}

/** Whether a task matches the search: every word is in its name, group or status word. */
export function matchesQuery(task: RegisteredTask, query: string): boolean {
  const words = query.trim().toLowerCase().split(/\s+/).filter(Boolean)
  const haystack =
    `${task.name} ${task.task_group ?? ''} ${STATUS_LOOK[task.status].word}`.toLowerCase()
  return words.every((word) => haystack.includes(word))
}
