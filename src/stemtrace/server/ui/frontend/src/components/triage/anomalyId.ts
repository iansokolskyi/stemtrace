import type { Anomaly } from '@/api/anomalies'

/** The `anomaly` entry id of the list of every anomaly (the Anomalies tile's). */
export const ALL_ANOMALIES_ID = 'all'

/** The `anomaly` entry id of one item: a task's anomaly is the task's, an unregistered one its name's. */
export const anomalyEntryId = (a: Anomaly): string =>
  a.kind === 'no_workers_online' ? a.kind : `${a.kind}:${a.task_id ?? a.name}`
