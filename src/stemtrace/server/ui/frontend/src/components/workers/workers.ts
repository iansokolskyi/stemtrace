import type { Worker } from '@/api/client'

/**
 * The process a host's page and row describe. A host can have several (a restart leaves the
 * old pid behind for a while): the online one wins, then the latest sighting.
 */
export function leadingWorker(processes: Worker[]): Worker {
  return [...processes].sort(
    (a, b) =>
      Number(b.status === 'online') - Number(a.status === 'online') ||
      Date.parse(b.last_seen) - Date.parse(a.last_seen),
  )[0]
}

/** One host: its processes and the leading one, whose status is the host's. */
export interface Host {
  hostname: string
  processes: Worker[]
  leading: Worker
}

/** The workers grouped by hostname, in list order. Status is decided per host, never per pid. */
export function hostsOf(workers: Worker[]): Host[] {
  const byName = new Map<string, Worker[]>()
  for (const worker of workers) {
    byName.set(worker.hostname, [...(byName.get(worker.hostname) ?? []), worker])
  }
  return [...byName]
    .map(([hostname, processes]) => ({ hostname, processes, leading: leadingWorker(processes) }))
    .sort((a, b) => a.hostname.localeCompare(b.hostname))
}

/** Hosts with no online process: what the sidebar badge counts. */
export function offlineHostCount(workers: Worker[]): number {
  return hostsOf(workers).filter((host) => host.leading.status === 'offline').length
}

/** Every task any of the host's processes registered, once each, sorted. */
export function registeredTasks(processes: Worker[]): string[] {
  return [...new Set(processes.flatMap((worker) => worker.registered_tasks))].sort((a, b) =>
    a.localeCompare(b),
  )
}

/** Table classes shared by the Workers list and the Worker page's panels. */
export const TH =
  'h-9 whitespace-nowrap border-b border-rule px-3 text-left text-xs font-semibold text-muted first:pl-4 last:pr-4'
export const TD = 'h-9 whitespace-nowrap px-3 text-xs first:pl-4 last:pr-4'
export const NUM = 'text-right tabular-nums'
