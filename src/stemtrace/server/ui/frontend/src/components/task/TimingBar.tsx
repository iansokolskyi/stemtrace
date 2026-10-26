import type { ReactElement } from 'react'
import { formatSpan } from '@/utils/format'
import { Panel } from './Panel'
import { stateFill } from './stateFill'

interface TimingBarProps {
  /** The task's current state; colours the runtime block. */
  state: string
  /** Time between publish and the worker starting the attempt; null when unknown. */
  queueWaitMs?: number | null
  /** The last run's time, queue wait excluded; null when unknown. */
  runtimeMs?: number | null
  /** All runs together; null when unknown. */
  totalRuntimeMs?: number | null
  /** The highest retry number the events report. */
  retries?: number
}

/**
 * Queue wait as a grey lead-in, then the runtime block, drawn to one scale. Shows only the
 * parts that are known, and nothing when neither is.
 */
export function TimingBar({
  state,
  queueWaitMs,
  runtimeMs,
  totalRuntimeMs,
  retries = 0,
}: TimingBarProps): ReactElement | null {
  const hasWait = typeof queueWaitMs === 'number'
  const hasRuntime = typeof runtimeMs === 'number'
  if (!hasWait && !hasRuntime) return null

  const wait = hasWait ? queueWaitMs : 0
  const runtime = hasRuntime ? runtimeMs : 0
  const scale = wait + runtime
  const waitPct = scale > 0 ? (wait / scale) * 100 : 0
  // Queue wait is the first attempt's and the runtime the last run's, so they are never summed;
  // the sum over every run is the API's `total_runtime_ms`.
  const ranMoreThanOnce =
    typeof totalRuntimeMs === 'number' && (retries > 0 || (hasRuntime && totalRuntimeMs > runtime))
  const label = [
    hasWait && `Queue wait ${formatSpan(wait)}`,
    hasRuntime && `last run ${formatSpan(runtime)}`,
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
          {hasRuntime && <Figure term="Last run" value={formatSpan(runtime)} />}
          {ranMoreThanOnce && <Figure term="All runs" value={formatSpan(totalRuntimeMs)} />}
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
