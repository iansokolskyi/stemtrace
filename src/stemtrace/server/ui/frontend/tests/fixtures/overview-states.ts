import type { Page, Route } from '@playwright/test'

/** Mock-API shapes for the Overview's error and empty states (spec and visual baselines). */

export const SUMMARY_ENDPOINTS = [
  /\/api\/overview(\?|$)/,
  /\/api\/stats\/timeseries(\?|$)/,
  /\/api\/failures(\?|$)/,
  /\/api\/stats\/tasks(\?|$)/,
  /\/api\/workers(\?|$)/,
]
export const FAILURES_ENDPOINT = SUMMARY_ENDPOINTS[2]
export const SERIES_ENDPOINT = SUMMARY_ENDPOINTS[1]

const json = (route: Route, body: unknown) =>
  route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) })

export type FailureMode = 'http' | 'network' | 'unauthorized'

/**
 * The endpoints answer with `mode` until `state.failing` is turned off, then the mock answers.
 * Routes added later win, so this can follow a page that has already loaded.
 */
export async function failSummaryEndpoints(
  page: Page,
  {
    endpoints = SUMMARY_ENDPOINTS,
    mode = 'http',
  }: { endpoints?: RegExp[]; mode?: FailureMode } = {},
): Promise<{ failing: boolean }> {
  const state = { failing: true }
  for (const endpoint of endpoints) {
    await page.route(endpoint, (route) => {
      if (!state.failing) return route.fallback()
      if (mode === 'network') return route.abort('failed')
      const status = mode === 'unauthorized' ? 401 : 500
      return route.fulfill({ status, contentType: 'application/json', body: '{"detail":"boom"}' })
    })
  }
  return state
}

/** Answers from the mock after `ms`: a request that is still in flight when a test looks. */
export async function delayEndpoints(page: Page, endpoints: RegExp[], ms: number): Promise<void> {
  for (const endpoint of endpoints) {
    await page.route(endpoint, async (route) => {
      await new Promise((resolve) => setTimeout(resolve, ms))
      await route.fallback()
    })
  }
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
      // The server sends 0.0 for a window with no runs, not null
      failure_rate: { value: 0, baseline: 0 },
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
