import { Link } from '@tanstack/react-router'
import { type ReactElement, useEffect, useState } from 'react'
import type { TaskNode } from '@/api/client'
import { Icon } from '@/components/Icon'
import { StatusChip } from '@/components/StatusChip'

const BUTTON =
  'inline-flex h-[30px] max-w-full items-center gap-1.5 rounded-[3px] px-3 text-xs font-semibold whitespace-nowrap'
const BUTTON_QUIET = `${BUTTON} bg-raised text-ink hover:bg-press`
const BUTTON_PRIMARY = `${BUTTON} bg-ink text-on-ink hover:opacity-90`

interface TaskHeaderProps {
  task: TaskNode
}

/**
 * The top of a task page: the last name segment as the title with the module path muted above
 * it, the state chip and the copyable ID on one line, and the links onward. A link whose target
 * is unknown is left out, never shown disabled.
 */
export function TaskHeader({ task }: TaskHeaderProps): ReactElement {
  const cut = task.name.lastIndexOf('.')
  const module = task.name.slice(0, Math.max(0, cut))
  const title = task.name.slice(cut + 1)
  // Every event of a run names the flow it belongs to; a task that has no events is its own root
  const rootId = task.events.find((event) => event.root_id)?.root_id ?? task.task_id

  return (
    <header data-testid="task-header" className="mb-4 flex min-w-0 flex-col items-start gap-2">
      {module && (
        <div
          data-task-module
          title={task.name}
          className="-mb-1.5 font-mono text-xs leading-snug text-muted [overflow-wrap:anywhere]"
        >
          {module}
        </div>
      )}
      <h1 className="text-xl font-bold leading-tight [overflow-wrap:anywhere]">{title}</h1>
      <div className="flex max-w-full flex-wrap items-center gap-x-3 gap-y-1.5">
        <StatusChip state={task.state} />
        <TaskIdentifier taskId={task.task_id} />
      </div>
      <div className="flex max-w-full flex-wrap gap-1.5">
        <Link to="/flows/$rootId" params={{ rootId }} className={BUTTON_PRIMARY}>
          <Icon name="flow" size={13} />
          Open flow
        </Link>
        {task.hostname && (
          <Link to="/executions" search={{ hostname: task.hostname }} className={BUTTON_QUIET}>
            <Icon name="server" size={13} className="flex-none" />
            <span className="min-w-0 truncate">Worker {task.hostname}</span>
          </Link>
        )}
        {task.node_type === 'TASK' && (
          <Link to="/executions" search={{ name_exact: task.name }} className={BUTTON_QUIET}>
            <Icon name="list" size={13} />
            All runs of this task
          </Link>
        )}
      </div>
    </header>
  )
}

/** The full task ID in mono with a button that copies it. */
function TaskIdentifier({ taskId }: { taskId: string }): ReactElement {
  const [copied, setCopied] = useState(false)

  useEffect(() => {
    if (!copied) return
    const timer = setTimeout(() => setCopied(false), 1500)
    return () => clearTimeout(timer)
  }, [copied])

  const copy = async (): Promise<void> => {
    try {
      await navigator.clipboard.writeText(taskId)
      setCopied(true)
    } catch {
      // Clipboard unavailable (insecure context or denied): the ID stays selectable text.
    }
  }

  return (
    <span className="inline-flex max-w-full items-center gap-1.5 font-mono text-xs text-muted">
      <span className="min-w-0 [overflow-wrap:anywhere]">{taskId}</span>
      <button
        type="button"
        onClick={copy}
        aria-label="Copy task ID"
        title="Copy task ID"
        className="inline-flex size-[26px] flex-none items-center justify-center rounded-[3px] bg-raised text-ink hover:bg-press"
      >
        <Icon name={copied ? 'check' : 'copy'} size={13} />
      </button>
      <span role="status" className="sr-only">
        {copied ? 'Copied' : ''}
      </span>
    </span>
  )
}
