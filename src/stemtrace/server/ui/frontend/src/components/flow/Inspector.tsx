import { Link } from '@tanstack/react-router'
import { type ReactElement, type ReactNode, useEffect, useState } from 'react'
import type { GraphNode, TaskNode } from '@/api/client'
import { useTask } from '@/api/queries'
import { Icon } from '@/components/Icon'
import { StatusChip } from '@/components/StatusChip'
import { TaskName } from '@/components/TaskName'
import { sortEventsChronologically } from '@/components/TimelineBar'
import { useCarriedRange } from '@/hooks/useCarriedRange'
import { formatSpan } from '@/utils/format'
import { type FlowLayout, lastNameSegment, notRunText, type PlacedContainer } from './layoutFlow'

const BUTTON =
  'inline-flex h-[30px] max-w-full items-center justify-center gap-1.5 rounded-[3px] px-3 text-xs font-semibold whitespace-nowrap'

/** At most this many kwargs are listed; the task page has them all. */
const MAX_KWARGS = 4

interface InspectorProps {
  nodes: Record<string, GraphNode>
  layout: FlowLayout
  /** The chosen node; none (or one the graph no longer holds) shows the empty text. */
  selectedId: string | null
  onToggleExpanded: (containerId: string) => void
}

/**
 * The right column of the flow page: what the chosen node is. A row without a value is left out
 * (never a dash). Wide screens: beside the diagram; narrow: below it.
 */
export function Inspector({
  nodes,
  layout,
  selectedId,
  onToggleExpanded,
}: InspectorProps): ReactElement {
  const node = selectedId ? nodes[selectedId] : undefined
  const box = node ? layout.containers.find((entry) => entry.id === node.task_id) : undefined
  return (
    <aside aria-label="Inspector" className="min-w-0 rounded-lg bg-surface">
      {!node ? (
        <p className="m-0 px-4 py-6 text-muted">Select a node to inspect it.</p>
      ) : (node.node_type ?? 'TASK') === 'TASK' ? (
        <TaskInspector key={node.task_id} node={node} />
      ) : (
        <ContainerInspector
          node={node}
          box={box}
          nodes={nodes}
          layout={layout}
          onToggleExpanded={onToggleExpanded}
        />
      )}
    </aside>
  )
}

function Header({ title, state }: { title: string; state: string }): ReactElement {
  return (
    <div className="flex items-center gap-3 px-4 pt-3.5 pb-2">
      <h2 className="m-0 text-sm font-bold">{title}</h2>
      <span className="grow" />
      <StatusChip state={state} />
    </div>
  )
}

interface Row {
  label: string
  value: ReactNode
  mono?: boolean
}

function Facts({ rows }: { rows: (Row | false | null | undefined | '')[] }): ReactElement | null {
  const shown = rows.filter((row): row is Row => Boolean(row))
  if (shown.length === 0) return null
  return (
    <dl className="m-0 grid grid-cols-[64px_minmax(0,1fr)] gap-x-3 gap-y-[7px] text-xs">
      {shown.map(({ label, value, mono }) => (
        <div key={label} className="contents">
          <dt className="text-muted">{label}</dt>
          <dd className={`m-0 [overflow-wrap:anywhere] ${mono ? 'font-mono' : ''}`}>{value}</dd>
        </div>
      ))}
    </dl>
  )
}

/** The kwargs of the first event that has any, as the API scrubbed them. */
function kwargsOf(task: TaskNode | undefined): [string, unknown][] {
  if (!task) return []
  const found = sortEventsChronologically(task.events ?? []).find(
    (event) => event.kwargs && Object.keys(event.kwargs).length > 0,
  )
  return Object.entries(found?.kwargs ?? {}).slice(0, MAX_KWARGS)
}

/** The message of the last failure or retry. */
function exceptionOf(task: TaskNode | undefined): string | undefined {
  if (!task) return undefined
  return (
    sortEventsChronologically(task.events ?? [])
      .filter((event) => event.state === 'FAILURE' || event.state === 'RETRY')
      .at(-1)?.exception ?? undefined
  )
}

function TaskInspector({ node }: { node: GraphNode }): ReactElement {
  const carried = useCarriedRange()
  // Kwargs and the exception message are not on graph nodes: they come from the task itself
  const { data } = useTask(node.task_id)
  const kwargs = kwargsOf(data?.task)
  const exception = exceptionOf(data?.task)
  return (
    <>
      <Header title="Inspector" state={node.state} />
      <div className="flex flex-col gap-3 px-4 pb-4">
        <div className="min-w-0">
          <TaskName name={node.name} />
        </div>
        <CopyId id={node.task_id} />
        <Facts
          rows={[
            node.hostname && {
              label: 'Worker',
              value: (
                <Link
                  to="/executions"
                  search={{ hostname: node.hostname, ...carried }}
                  className="underline decoration-muted underline-offset-2 hover:decoration-ink"
                >
                  {node.hostname}
                </Link>
              ),
            },
            node.queue && { label: 'Queue', value: node.queue, mono: true },
            node.queue_wait_ms != null && {
              label: 'Wait',
              value: formatSpan(node.queue_wait_ms),
              mono: true,
            },
            node.runtime_ms != null && {
              label: 'Runtime',
              value: formatSpan(node.runtime_ms),
              mono: true,
            },
          ]}
        />
        {exception && (
          <div>
            <div className="text-muted">Exception</div>
            <div className="font-mono text-xs text-failure-t [overflow-wrap:anywhere]">
              {exception}
            </div>
          </div>
        )}
        {kwargs.length > 0 && (
          <div>
            <div className="mb-1 text-muted">Kwargs</div>
            <dl className="m-0 grid grid-cols-[minmax(0,max-content)_minmax(0,1fr)] gap-x-3 gap-y-1 font-mono text-xs">
              {kwargs.map(([key, value]) => (
                <div key={key} className="contents">
                  <dt className="text-muted [overflow-wrap:anywhere]">{key}</dt>
                  <dd className="m-0 [overflow-wrap:anywhere]">{JSON.stringify(value)}</dd>
                </div>
              ))}
            </dl>
          </div>
        )}
        <Link
          to="/tasks/$taskId"
          params={{ taskId: node.task_id }}
          className={`${BUTTON} bg-ink text-on-ink hover:opacity-90`}
        >
          Open full detail
        </Link>
      </div>
    </>
  )
}

interface ContainerInspectorProps {
  node: GraphNode
  box: PlacedContainer | undefined
  nodes: Record<string, GraphNode>
  layout: FlowLayout
  onToggleExpanded: (containerId: string) => void
}

function ContainerInspector({
  node,
  box,
  nodes,
  layout,
  onToggleExpanded,
}: ContainerInspectorProps): ReactElement {
  const callback = box?.callbackId ? nodes[box.callbackId] : undefined
  const notRun = layout.notRun.find((entry) => entry.chordId === node.task_id)
  const parts = box
    ? [
        `${box.succeeded} succeeded`,
        box.failed > 0 && `${box.failed} failed`,
        box.other > 0 && `${box.other} not done`,
      ].filter(Boolean)
    : []
  return (
    <>
      <Header title={node.node_type === 'CHORD' ? 'Chord' : 'Group'} state={node.state} />
      <div className="flex flex-col gap-3 px-4 pb-4">
        <Facts
          rows={[
            box && {
              label: 'Members',
              value: `${box.memberCount} ${box.memberCount === 1 ? 'task' : 'tasks'}: ${parts.join(', ')}`,
            },
            callback && {
              label: 'Callback',
              value: (
                <span className="flex flex-col items-start gap-1">
                  <span className="font-mono">{lastNameSegment(callback.name)}</span>
                  <StatusChip state={callback.state} />
                </span>
              ),
            },
            !callback &&
              (notRun ?? node.not_run_reason) && {
                label: 'Callback',
                value: (
                  <span className="text-muted">
                    Not run:{' '}
                    {notRun?.text ??
                      notRunText(
                        node.not_run_reason ?? 'header_failed',
                        box?.failed ?? 0,
                        box?.memberCount ?? 0,
                      )}
                  </span>
                ),
              },
            node.duration_ms != null && { label: 'Duration', value: formatSpan(node.duration_ms) },
          ]}
        />
        {box?.collapsible && (
          <button
            type="button"
            onClick={() => onToggleExpanded(node.task_id)}
            className={`${BUTTON} bg-raised text-ink hover:bg-press`}
          >
            {box.expanded ? 'Collapse members' : `Expand all ${box.memberCount} members`}
          </button>
        )}
      </div>
    </>
  )
}

/** The full task ID in mono with a button that copies it. */
function CopyId({ id }: { id: string }): ReactElement {
  const [copied, setCopied] = useState(false)

  useEffect(() => {
    if (!copied) return
    const timer = setTimeout(() => setCopied(false), 1500)
    return () => clearTimeout(timer)
  }, [copied])

  const copy = async (): Promise<void> => {
    try {
      await navigator.clipboard.writeText(id)
      setCopied(true)
    } catch {
      // Clipboard unavailable (insecure context or denied): the ID stays selectable text.
    }
  }

  return (
    <span className="inline-flex max-w-full items-center gap-1.5 font-mono text-xs text-muted">
      <span className="min-w-0 [overflow-wrap:anywhere]">{id}</span>
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
