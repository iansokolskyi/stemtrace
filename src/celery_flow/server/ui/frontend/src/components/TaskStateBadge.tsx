/**
 * Badge component for displaying task state.
 */

import { clsx } from 'clsx'

interface TaskStateBadgeProps {
  state: string
  className?: string
}

const stateClasses: Record<string, string> = {
  PENDING: 'task-state-pending',
  RECEIVED: 'task-state-received',
  STARTED: 'task-state-started',
  SUCCESS: 'task-state-success',
  FAILURE: 'task-state-failure',
  RETRY: 'task-state-retry',
  REVOKED: 'task-state-revoked',
  REJECTED: 'task-state-rejected',
}

export function TaskStateBadge({ state, className }: TaskStateBadgeProps) {
  const stateClass = stateClasses[state] ?? 'task-state-pending'

  return <span className={clsx('task-state-badge', stateClass, className)}>{state}</span>
}
