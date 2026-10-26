import type { TaskNode } from '@/api/client'

/** The highest retry number any event reports; 0 when the task never retried. */
export function retryCount(task: TaskNode): number {
  return task.events.reduce((most, event) => Math.max(most, event.retries ?? 0), 0)
}
