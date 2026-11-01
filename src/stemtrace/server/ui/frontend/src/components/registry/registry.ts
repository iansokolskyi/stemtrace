import type { RegisteredTask, TaskStatus } from '@/api/client'
import type { IconName } from '@/components/Icon'

/** What each status is called and drawn as. Only `not_registered` is loud: it is the problem. */
export const STATUS_LOOK: Record<TaskStatus, { word: string; icon: IconName; loud: boolean }> = {
  active: { word: 'Active', icon: 'check', loud: false },
  never_run: { word: 'Never run', icon: 'clock', loud: false },
  not_registered: { word: 'Not registered', icon: 'alert', loud: true },
}

/** The sentence under a name that explains its status; none for an active task. */
export function noteOf(task: RegisteredTask): string | null {
  if (task.status === 'not_registered') {
    return 'No worker has this task registered. New runs with this name may stay in Pending.'
  }
  if (task.status === 'never_run') {
    return task.registered_by.length > 0
      ? `Registered by ${task.registered_by.join(', ')}; it has not run yet.`
      : 'It has not run yet.'
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
