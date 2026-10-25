import { clsx } from 'clsx'
import type { ReactElement } from 'react'

interface TaskNameProps {
  /** The full dotted task name. */
  name: string
  className?: string
}

/**
 * A task name in mono: the module path muted, the last segment in semibold. When the room is
 * short, the module path is ellipsised (the end of the name stays), so the task itself is always
 * readable. The text content is still the full name, and `title` has it for hover.
 */
export function TaskName({ name, className }: TaskNameProps): ReactElement {
  const cut = name.lastIndexOf('.')
  const module = cut < 0 ? '' : name.slice(0, cut + 1)
  const last = name.slice(cut + 1)
  return (
    <span
      title={name}
      className={clsx('flex max-w-full min-w-0 font-mono text-xs leading-5', className)}
    >
      {module && (
        <span data-task-module className="min-w-0 truncate text-muted">
          {module}
        </span>
      )}
      <span data-task-last className="max-w-full shrink-0 truncate font-semibold">
        {last}
      </span>
    </span>
  )
}
