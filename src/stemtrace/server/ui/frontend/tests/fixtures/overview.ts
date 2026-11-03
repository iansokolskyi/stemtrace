/**
 * The Overview's endpoints for the mock suite: GET /overview, /stats/timeseries (without a
 * split), /failures and /stats/tasks with its group rollups. They model the server's shapes
 * (see `server/api/summary_routes.py`); the tasks the panel lists come from `setupMockApi`.
 */

import type { Page, Route } from '@playwright/test'
import { createFailedTask, createSuccessTask, type MockTask } from './mock-data'

const FIVE_MINUTES = 300_000

export interface Bucket {
  success: number
  failure: number
  retry: number
}

export interface OverviewScenario {
  /** Start of the first bucket, ms. */
  start: number
  /** Five-minute buckets, oldest first. */
  buckets: Bucket[]
  failures: {
    exception_type: string | null
    count: number
    task_names: string[]
  }[]
  /** Per task: name, group, runs, failures, p50/p95 ms. */
  tasks: { name: string; group: string; runs: number; failures: number; p50: number; p95: number }[]
  overview: {
    runs_per_minute: [number, number]
    failures: [number, number]
    failure_rate: [number, number]
  }
}

const iso = (ms: number): string => new Date(ms).toISOString()

/** The start of the 5-minute bucket `index` of a scenario. */
export const bucketStart = (scenario: OverviewScenario, index: number): number =>
  scenario.start + index * FIVE_MINUTES

/** Twelve buckets ending now, with one failure spike (bucket 8) and a quiet rest. */
export function spikeScenario(now = Date.now()): OverviewScenario {
  const end = Math.floor(now / FIVE_MINUTES) * FIVE_MINUTES + FIVE_MINUTES
  const buckets: Bucket[] = Array.from({ length: 12 }, () => ({
    success: 40,
    failure: 0,
    retry: 0,
  }))
  buckets[8] = { success: 22, failure: 14, retry: 3 }
  buckets[3] = { success: 38, failure: 1, retry: 2 }
  return {
    start: end - 12 * FIVE_MINUTES,
    buckets,
    failures: [
      { exception_type: 'ValueError', count: 14, task_names: ['tests.flow.step_2'] },
      {
        exception_type: 'TimeoutError',
        count: 1,
        task_names: ['tests.other.fetch', 'tests.flow.step_2'],
      },
      { exception_type: null, count: 2, task_names: ['tests.other.fetch'] },
    ],
    tasks: [
      {
        name: 'tests.flow.step_2',
        group: 'tests.flow',
        runs: 120,
        failures: 15,
        p50: 310,
        p95: 2400,
      },
      { name: 'tests.flow.step_1', group: 'tests.flow', runs: 120, failures: 0, p50: 90, p95: 400 },
      {
        name: 'tests.other.fetch',
        group: 'tests.other',
        runs: 300,
        failures: 3,
        p50: 40,
        p95: 120,
      },
    ],
    overview: { runs_per_minute: [8.4, 7.9], failures: [18, 4], failure_rate: [0.04, 0.01] },
  }
}

/**
 * The tasks behind the spike bucket: a flow root that succeeded and its child that failed inside
 * bucket 8 with `ValueError`, plus one quiet success elsewhere.
 */
export function spikeTasks(scenario: OverviewScenario): {
  tasks: MockTask[]
  failedId: string
  rootId: string
} {
  const at = bucketStart(scenario, 8) + 2 * 60_000
  const root = createSuccessTask('tests.flow.step_1', { ok: true })
  const failed = createFailedTask('tests.flow.step_2', 'ValueError: bad input', 'Traceback: ...')
  // Fixed IDs: the panel prints them, so snapshots must not depend on a random one
  for (const [task, id] of [
    [root, '5c0cf6cc-1d1e-4a0b-9a5e-0a1b2c3d4e5f'],
    [failed, '7a560c1a-4b1f-41c8-b138-567ecc59e373'],
  ] as const) {
    task.task_id = id
    task.root_id = root.task_id
    for (const event of task.events) {
      event.task_id = id
      event.root_id = root.task_id
    }
  }
  root.children = [failed.task_id]
  root.last_updated = iso(at - 30_000)
  root.first_seen = iso(at - 40_000)
  failed.parent_id = root.task_id
  failed.root_id = root.task_id
  failed.last_updated = iso(at)
  failed.first_seen = iso(at - 20_000)
  failed.hostname = 'worker-1'
  failed.queue = 'default'
  failed.task_group = 'tests.flow'
  failed.runtime_ms = 1800
  failed.queue_wait_ms = 120
  const quiet = createSuccessTask('tests.other.fetch', 1)
  quiet.last_updated = iso(bucketStart(scenario, 1) + 60_000)
  return { tasks: [root, failed, quiet], failedId: failed.task_id, rootId: root.task_id }
}

const json = (route: Route, body: unknown, status = 200) =>
  route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })

/** Serve the Overview's endpoints from `scenario`; everything else falls through to the mock API. */
export async function mockOverview(page: Page, scenario: OverviewScenario): Promise<URL[]> {
  const requests: URL[] = []
  const end = scenario.start + scenario.buckets.length * FIVE_MINUTES
  const window = { start: iso(scenario.start), end: iso(end) }
  const column = (pick: (b: Bucket) => number): number[] => scenario.buckets.map(pick)

  await page.route(/\/api\/overview(\?|$)/, (route) => {
    requests.push(new URL(route.request().url()))
    const { runs_per_minute: rpm, failures, failure_rate: rate } = scenario.overview
    return json(route, {
      window,
      baseline_window: { start: iso(scenario.start - 12 * FIVE_MINUTES), end: window.start },
      runs: { value: 480, baseline: 470 },
      failures: { value: failures[0], baseline: failures[1] },
      runs_per_minute: { value: rpm[0], baseline: rpm[1] },
      failure_rate: { value: rate[0], baseline: rate[1] },
      workers: { online: 1, total: 2 },
    })
  })

  await page.route(/\/api\/stats\/timeseries(\?|$)/, (route) => {
    const url = new URL(route.request().url())
    if (url.searchParams.has('group_by')) return route.fallback()
    requests.push(url)
    return json(route, {
      window,
      bucket_seconds: 300,
      starts: scenario.buckets.map((_, i) => iso(scenario.start + i * FIVE_MINUTES)),
      counts: {
        SUCCESS: column((b) => b.success),
        FAILURE: column((b) => b.failure),
        RETRY: column((b) => b.retry),
      },
    })
  })

  await page.route(/\/api\/failures(\?|$)/, (route) => {
    requests.push(new URL(route.request().url()))
    return json(route, {
      window,
      group_by: 'exception',
      total: scenario.failures.reduce((sum, f) => sum + f.count, 0),
      groups: scenario.failures.map((f) => ({
        ...f,
        first_seen: iso(end - 50 * 60_000),
        last_seen: iso(end - 6 * 60_000),
        sample_task_ids: [],
      })),
    })
  })

  await page.route(/\/api\/stats\/tasks(\?|$)/, (route) => {
    requests.push(new URL(route.request().url()))
    const row = (t: OverviewScenario['tasks'][number]) => ({
      name: t.name,
      task_group: t.group,
      runs: t.runs,
      failures: t.failures,
      failure_rate: t.failures / t.runs,
      runtime_samples: t.runs,
      p50_ms: t.p50,
      p95_ms: t.p95,
      last_run: iso(end - 60_000),
      last_state: 'SUCCESS',
    })
    const groups = [...new Set(scenario.tasks.map((t) => t.group))].map((name) => {
      const members = scenario.tasks.filter((t) => t.group === name)
      const runs = members.reduce((s, t) => s + t.runs, 0)
      const failures = members.reduce((s, t) => s + t.failures, 0)
      return {
        name,
        task_count: members.length,
        runs,
        failures,
        failure_rate: failures / runs,
        runtime_samples: runs,
        p50_ms: 100,
        p95_ms: Math.max(...members.map((t) => t.p95)),
        last_run: iso(end - 60_000),
      }
    })
    return json(route, { window, tasks: scenario.tasks.map(row), groups })
  })

  return requests
}
