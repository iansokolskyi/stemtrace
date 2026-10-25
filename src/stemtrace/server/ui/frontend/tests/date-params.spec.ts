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
    ['/', 'Tasks'],
    ['/graphs', 'Task Graphs'],
  ] as const) {
    test(`${path} with from_date=${value} renders and ignores the date`, async ({ page }) => {
      test.skip(isRealMode, 'Mock-only test')
      const errors: string[] = []
      page.on('pageerror', (e) => errors.push(e.message))
      const apiDates: (string | null)[] = []
      page.on('request', (r) => {
        const url = new URL(r.url())
        if (url.pathname.startsWith('/api/')) apiDates.push(url.searchParams.get('from_date'))
      })
      await setupMockApi(page)
      await page.goto(`${path}?from_date=${value}&to_date=${value}`)
      await expect(page.getByRole('heading', { level: 1, name: heading })).toBeVisible()
      expect(errors).toEqual([])
      expect(apiDates.filter((d) => d !== null)).toEqual([])
    })
  }
}

test('a real date in the URL is still applied', async ({ page }) => {
  test.skip(isRealMode, 'Mock-only test')
  const apiDates: (string | null)[] = []
  page.on('request', (r) => {
    const url = new URL(r.url())
    if (url.pathname === '/api/tasks') apiDates.push(url.searchParams.get('from_date'))
  })
  await setupMockApi(page)
  await page.goto('/?from_date=2026-01-01')
  await expect(page.getByRole('heading', { level: 1, name: 'Tasks' })).toBeVisible()
  await expect.poll(() => apiDates.some((d) => d?.startsWith('2026-01-01'))).toBe(true)
})
