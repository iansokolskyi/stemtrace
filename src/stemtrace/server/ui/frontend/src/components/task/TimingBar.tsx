import type { ReactElement } from 'react'
import { formatSpan } from '@/utils/format'
import { Panel } from './Panel'
import { stateFill } from './stateFill'

interface TimingBarProps {
  /** The task's current state; colours the runtime block. */
  state: string
  /** Time between publish and the worker starting the attempt; null when unknown. */
  queueWaitMs?: number | null
  /** Time spent running, queue wait excluded; null when unknown. */
  runtimeMs?: number | null
}

/**
 * Queue wait as a grey lead-in, then the runtime block, drawn to one scale. Shows only the
 * parts that are known, and nothing when neither is.
 */
export function TimingBar({ state, queueWaitMs, runtimeMs }: TimingBarProps): ReactElement | null {
  const hasWait = typeof queueWaitMs === 'number'
  const hasRuntime = typeof runtimeMs === 'number'
  if (!hasWait && !hasRuntime) return null

  const wait = hasWait ? queueWaitMs : 0
  const runtime = hasRuntime ? runtimeMs : 0
  const total = wait + runtime
  const waitPct = total > 0 ? (wait / total) * 100 : 0
  const label = [
    hasWait && `Queue wait ${formatSpan(wait)}`,
    hasRuntime && `runtime ${formatSpan(runtime)}`,
  ]
    .filter(Boolean)
    .join(', ')

  return (
    <Panel title="Timing">
      <div className="px-4 pt-2 pb-3.5">
        <div
          role="img"
          aria-label={label}
          className="flex h-[22px] items-center rounded-[3px] bg-raised"
        >
          {hasWait && (
            <div
              data-testid="timing-queue-wait"
              className="h-1.5 min-w-[3px] rounded-l-[3px] bg-track"
              style={{ width: `${waitPct}%` }}
            />
          )}
          {hasRuntime && (
            <div
              data-testid="timing-runtime"
              className={`h-full min-w-[3px] rounded-[3px] ${stateFill(state)}`}
              style={{ width: `${100 - waitPct}%` }}
            />
          )}
        </div>
        <dl className="mt-2 flex flex-wrap gap-x-5 gap-y-1 text-xs">
          {hasWait && <Figure term="Queue wait" value={formatSpan(wait)} />}
          {hasRuntime && <Figure term="Runtime" value={formatSpan(runtime)} />}
          {hasWait && hasRuntime && <Figure term="Total" value={formatSpan(total)} />}
        </dl>
      </div>
    </Panel>
  )
}

function Figure({ term, value }: { term: string; value: string }): ReactElement {
  return (
    <div className="flex gap-1.5">
      <dt className="text-muted">{term}</dt>
      <dd className="font-bold">{value}</dd>
    </div>
  )
}
