import { createMockEvent, type MockTask } from './mock-data'

/** Runs for the Executions Timeline view, with times in seconds after T0 (23:58:00 UTC). */
export const T0 = Date.parse('2026-10-17T23:58:00Z')
export const at = (seconds: number): string => new Date(T0 + seconds * 1000).toISOString()

export interface Step {
  state: string
  s: number
  retries?: number
}

/** A run whose events are `steps` (seconds after T0); first_seen and last_updated follow them. */
export function run(id: string, name: string, state: string, steps: Step[]): MockTask {
  const first = steps[0].s
  const last = steps[steps.length - 1].s
  return {
    task_id: id,
    name,
    state,
    parent_id: null,
    root_id: id,
    node_type: 'TASK',
    group_id: null,
    chord_id: null,
    children: [],
    first_seen: at(first),
    last_updated: at(last),
    duration_ms: (last - first) * 1000,
    events: steps.map((step) =>
      createMockEvent(id, name, step.state, {
        timestamp: at(step.s),
        root_id: id,
        retry_count: step.retries ?? 0,
        retries: step.retries ?? 0,
      }),
    ),
  }
}

/** Success, failure and retry runs over 8 seconds: 23:58:00 to 23:58:08. */
export function threeRuns(): MockTask[] {
  return [
    run('aaaa0000-0000-4000-8000-000000000001', 'shop.orders.reconcile', 'SUCCESS', [
      { state: 'PENDING', s: 0 },
      { state: 'STARTED', s: 2 },
      { state: 'SUCCESS', s: 5 },
    ]),
    run('bbbb0000-0000-4000-8000-000000000002', 'shop.mail.send_receipt', 'FAILURE', [
      { state: 'PENDING', s: 1 },
      { state: 'STARTED', s: 3 },
      { state: 'FAILURE', s: 4 },
    ]),
    run('cccc0000-0000-4000-8000-000000000003', 'shop.sync.pull', 'RETRY', [
      { state: 'PENDING', s: 2 },
      { state: 'STARTED', s: 3 },
      { state: 'RETRY', s: 4, retries: 1 },
      { state: 'STARTED', s: 6, retries: 1 },
      { state: 'RETRY', s: 8, retries: 1 },
    ]),
  ]
}
