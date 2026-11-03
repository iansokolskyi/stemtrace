import type { Anomaly, AnomalyEvidence, AnomalyKind } from '@/api/anomalies'
import { formatAgo, formatSpan, shortHost } from '@/utils/format'

/** The word an anomaly kind is shown with (never colour alone: the chip pairs it with an icon). */
export const KIND_WORD: Record<AnomalyKind, string> = {
  stuck: 'Stuck',
  interrupted: 'Interrupted',
  unregistered: 'Unregistered',
  no_workers_online: 'No workers online',
}

const KIND_HEADING: Record<AnomalyKind, string> = {
  stuck: 'Stuck: running far longer than usual',
  interrupted: 'Interrupted: the worker went away mid-task',
  unregistered: 'Unregistered: no worker knows this task',
  no_workers_online: 'No workers online: nothing can run the waiting tasks',
}

/**
 * The callout heading of an anomaly, as the prototype words it. "Longer than usual" needs a
 * usual: with no p95 (too few runs) the limit is a default, so the heading names the limit.
 */
export function kindHeading(anomaly: Anomaly): string {
  if (anomaly.kind === 'stuck' && anomaly.p95_ms === null) return 'Stuck: running past its limit'
  return KIND_HEADING[anomaly.kind]
}

const STOPPED: Record<AnomalyEvidence, string> = {
  worker_shutdown: 'shut down',
  worker_terminating: 'was terminated',
  worker_restarted: 'restarted',
  cancelled_retry: 'stopped and cancelled the task',
}

const tasks = (count: number): string => `${count} ${count === 1 ? 'task' : 'tasks'}`

/** Why the item is flagged, in a sentence built from the API's fields (the server sends no text). */
export function explain(anomaly: Anomaly): string {
  const host = anomaly.hostname ? shortHost(anomaly.hostname) : null
  switch (anomaly.kind) {
    case 'stuck': {
      const parts = [`No result seen for ${formatSpan(anomaly.elapsed_ms)}`]
      if (host) parts[0] += ` on ${host}`
      let limit = `the limit for this task is ${formatSpan(anomaly.threshold_ms)}`
      if (anomaly.p95_ms !== null) {
        limit += ` (p95 ${formatSpan(anomaly.p95_ms)} over ${anomaly.runtime_samples} runs)`
      }
      return `${parts[0]}; ${limit}.`
    }
    case 'interrupted': {
      const verb = anomaly.evidence ? STOPPED[anomaly.evidence] : 'stopped'
      return `${host ?? 'The worker'} ${verb} while this task was running; no result seen since.`
    }
    case 'unregistered': {
      const base = `${tasks(anomaly.task_count)} PENDING; no online worker registers this name`
      return anomaly.similar_name ? `${base} (renamed from ${anomaly.similar_name}?).` : `${base}.`
    }
    case 'no_workers_online':
      return `${tasks(anomaly.task_count)} waiting and no worker is online to run them.`
  }
}

/** `11:20 UTC, 38m ago`: the start of the item in UTC, so a report reads the same everywhere. */
export function sinceText(since: string): string {
  const clock = new Date(since).toISOString().slice(11, 16)
  return `${clock} UTC, ${formatAgo(since)}`
}

/** Shared look of the anomaly links, the same as the task page's quiet buttons. */
export const ACTION =
  'inline-flex h-[30px] min-w-0 max-w-full items-center gap-1.5 rounded-[3px] bg-raised px-3 text-xs font-semibold whitespace-nowrap text-ink hover:bg-press'
