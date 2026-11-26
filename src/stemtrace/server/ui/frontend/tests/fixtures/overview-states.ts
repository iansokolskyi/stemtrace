import type { Page, Route } from '@playwright/test'

/** Mock-API shapes for the Overview's error and empty states (spec and visual baselines). */

const SUMMARY_ENDPOINTS = [
  /\/api\/overview(\?|$)/,
  /\/api\/stats\/timeseries(\?|$)/,
  /\/api\/failures(\?|$)/,
  /\/api\/stats\/tasks(\?|$)/,
  /\/api\/workers(\?|$)/,
]

const json = (route: Route, body: unknown) =>
  route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) })

/** Every summary endpoint answers 500 until `state.failing` is turned off, then the mock answers. */
export async function failSummaryEndpoints(page: Page): Promise<{ failing: boolean }> {
  const state = { failing: true }
  for (const endpoint of SUMMARY_ENDPOINTS) {
    await page.route(endpoint, (route) =>
      state.failing
        ? route.fulfill({ status: 500, contentType: 'application/json', body: '{"detail":"boom"}' })
        : route.fallback(),
    )
  }
  return state
}

/** A server that holds no runs and no workers: every summary endpoint answers, with nothing. */
export async function emptySummaryEndpoints(page: Page): Promise<void> {
  const window = { start: '2026-10-17T06:00:00Z', end: '2026-10-17T12:00:00Z' }
  await page.route(/\/api\/overview(\?|$)/, (route) =>
    json(route, {
      window,
      baseline_window: { start: '2026-10-17T00:00:00Z', end: window.start },
      runs: { value: 0, baseline: 0 },
      failures: { value: 0, baseline: 0 },
      runs_per_minute: { value: 0, baseline: 0 },
      failure_rate: { value: null, baseline: null },
      workers: { online: 0, total: 0 },
    }),
  )
  await page.route(/\/api\/stats\/timeseries(\?|$)/, (route) =>
    json(route, { window, bucket_seconds: 300, starts: [], counts: {} }),
  )
  await page.route(/\/api\/failures(\?|$)/, (route) =>
    json(route, { window, group_by: 'exception', total: 0, groups: [] }),
  )
  await page.route(/\/api\/stats\/tasks(\?|$)/, (route) =>
    json(route, { window, groups: [], tasks: [] }),
  )
  await page.route(/\/api\/workers(\?|$)/, (route) => json(route, { workers: [], total: 0 }))
}
