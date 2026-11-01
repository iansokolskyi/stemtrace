import { expect, test } from '@playwright/test'

import { setupMockApi } from './fixtures/mock-api'

/**
 * A date filter in the URL that isn't a real YYYY-MM-DD date is ignored instead of
 * crashing the page (the date picker threw on an Invalid Date and unmounted the app).
 */

const isRealMode = process.env.E2E_MODE === 'real'

const BAD_DATES = ['abc', '2026', '2026-13-45', '2026-02-30', '2026-1-5x', '20260101']

for (const value of BAD_DATES) {
  for (const [path, heading] of [
    ['/', 'Executions'],
    ['/graphs', 'Flows'],
  ] as const) {
    test(`${path} with from_date=${value} renders and ignores the date`, async ({ page }) => {
      test.skip(isRealMode, 'Mock-only test')
      const errors: string[] = []
      page.on('pageerror', (e) => errors.push(e.message))
      const apiDates: (string | null)[] = []
      page.on('request', (r) => {
        const url = new URL(r.url())
        if (url.pathname.startsWith('/api/')) {
          apiDates.push(url.searchParams.get('from_date'), url.searchParams.get('to_date'))
        }
      })
      await setupMockApi(page)
      await page.goto(`${path}?from_date=${value}&to_date=${value}`)
      await expect(page.getByRole('heading', { level: 1, name: heading })).toBeVisible()
      expect(errors).toEqual([])
      expect(apiDates.filter((d) => d !== null)).toEqual([])
    })
  }
}

for (const [path, endpoint, heading] of [
  ['/', '/api/tasks', 'Executions'],
  ['/graphs', '/api/graphs', 'Flows'],
] as const) {
  test(`${path} still applies real from_date and to_date`, async ({ page }) => {
    test.skip(isRealMode, 'Mock-only test')
    const sent: { from: string | null; to: string | null; within: string | null }[] = []
    page.on('request', (r) => {
      const url = new URL(r.url())
      if (url.pathname === endpoint) {
        sent.push({
          from: url.searchParams.get('from_date'),
          to: url.searchParams.get('to_date'),
          within: url.searchParams.get('within'),
        })
      }
    })
    await setupMockApi(page)
    await page.goto(`${path}?from_date=2026-01-01&to_date=2026-01-31`)
    await expect(page.getByRole('heading', { level: 1, name: heading })).toBeVisible()
    await expect
      .poll(() =>
        sent.some(
          (s) =>
            s.from?.startsWith('2026-01-01') && s.to?.startsWith('2026-01-31') && s.within === null,
        ),
      )
      .toBe(true)
  })
}
