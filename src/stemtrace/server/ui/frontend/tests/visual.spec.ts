import { expect, type Page, test } from '@playwright/test'

import { setupMockApi } from './fixtures/mock-api'

/**
 * Visual snapshots of the Signal box frame. Runs only in the `visual` project (PW_VISUAL=1),
 * inside the pinned Playwright image, so font rendering matches the stored baselines:
 *   npm run test:visual          # compare
 *   npm run test:visual:update   # regenerate after an intended change, then review each PNG
 */

type Theme = 'light' | 'dark'

/** Fixed clock, stored theme, viewport, fonts loaded, network idle; then compare. */
async function snapshot(
  page: Page,
  name: string,
  { theme, width, path }: { theme: Theme; width: number; path: string },
): Promise<void> {
  await page.clock.setFixedTime(new Date('2026-10-17T12:00:00Z'))
  await page.addInitScript((t) => localStorage.setItem('stemtrace-theme', t), theme)
  await page.setViewportSize({ width, height: 900 })
  await page.goto(path)
  await page.waitForLoadState('networkidle')
  await page.evaluate(() => document.fonts.ready)
  await expect(page).toHaveScreenshot(`${name}.png`, { fullPage: true })
}

for (const theme of ['light', 'dark'] as const) {
  for (const width of [1280, 400]) {
    test(`frame on workers, ${theme}, ${width}px`, async ({ page }) => {
      await page.routeWebSocket(/\/ws$/, () => {})
      await setupMockApi(page)
      await snapshot(page, `frame-${theme}-${width}`, { theme, width, path: '/workers' })
    })
  }
}

test('drawer open at 400px, dark', async ({ page }) => {
  await page.routeWebSocket(/\/ws$/, () => {})
  await setupMockApi(page)
  await page.addInitScript(() => localStorage.setItem('stemtrace-theme', 'dark'))
  await page.setViewportSize({ width: 400, height: 900 })
  await page.clock.setFixedTime(new Date('2026-10-17T12:00:00Z'))
  await page.goto('/workers')
  await page.getByRole('button', { name: 'Open navigation' }).click()
  await expect(page.getByRole('complementary', { name: 'Primary navigation' })).toBeInViewport({
    ratio: 1,
  })
  await page.evaluate(() => document.fonts.ready)
  await expect(page).toHaveScreenshot('drawer-dark-400.png')
})
