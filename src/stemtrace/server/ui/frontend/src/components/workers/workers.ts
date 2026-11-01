import type { Worker } from '@/api/client'

/** The workers in list order: by hostname, then pid. */
export function sortedWorkers(workers: Worker[]): Worker[] {
  return [...workers].sort((a, b) => a.hostname.localeCompare(b.hostname) || a.pid - b.pid)
}

/**
 * The process a host's page describes. A host can have several (a restart leaves the old pid
 * behind): the online one wins, then the latest sighting.
 */
export function leadingWorker(processes: Worker[]): Worker {
  return [...processes].sort(
    (a, b) =>
      Number(b.status === 'online') - Number(a.status === 'online') ||
      Date.parse(b.last_seen) - Date.parse(a.last_seen),
  )[0]
}

/** Every task any of the host's processes registered, once each, sorted. */
export function registeredTasks(processes: Worker[]): string[] {
  return [...new Set(processes.flatMap((worker) => worker.registered_tasks))].sort()
}

/** Table classes shared by the Workers list and the Worker page's panels. */
export const TH =
  'h-9 whitespace-nowrap border-b border-rule px-3 text-left text-xs font-semibold text-muted first:pl-4 last:pr-4'
export const TD = 'h-9 whitespace-nowrap px-3 text-xs first:pl-4 last:pr-4'
export const NUM = 'text-right tabular-nums'
