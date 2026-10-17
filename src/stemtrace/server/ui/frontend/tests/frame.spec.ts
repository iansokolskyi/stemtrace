import { expect, test } from '@playwright/test'

import { setupMockApi } from './fixtures/mock-api'

/**
 * The Signal box frame: theme, sidebar, top strip, drawer, badges and live status.
 * Mock mode only (it controls storage, the OS colour scheme and the API).
 */

const isRealMode = process.env.E2E_MODE === 'real'

const DARK_BG = 'rgb(26, 28, 30)' // --bg #1A1C1E
const LIGHT_BG = 'rgb(239, 240, 241)' // --bg #EFF0F1

async function bodyBackground(page: import('@playwright/test').Page): Promise<string> {
  return page.evaluate(() => getComputedStyle(document.body).backgroundColor)
}

test.describe('Theme', () => {
  test.skip(isRealMode, 'Mock-only test')

  test('theme follows the OS when nothing is stored', async ({ page }) => {
    await setupMockApi(page)
    await page.emulateMedia({ colorScheme: 'dark' })
    await page.goto('/executions')
    await expect.poll(() => bodyBackground(page)).toBe(DARK_BG)
    await page.emulateMedia({ colorScheme: 'light' })
    await expect.poll(() => bodyBackground(page)).toBe(LIGHT_BG)
  })

  test('theme toggle overrides the OS and survives reload', async ({ page }) => {
    await setupMockApi(page)
    await page.emulateMedia({ colorScheme: 'light' })
    await page.goto('/executions')
    await page.getByRole('button', { name: 'Switch to dark theme' }).click()
    await expect.poll(() => bodyBackground(page)).toBe(DARK_BG)
    await page.reload()
    await expect.poll(() => bodyBackground(page)).toBe(DARK_BG)
    expect(await page.evaluate(() => localStorage.getItem('stemtrace-theme'))).toBe('dark')
    await expect(page.getByRole('button', { name: 'Switch to light theme' })).toBeVisible()
  })

  test('stored theme applies before first paint', async ({ page }) => {
    await setupMockApi(page)
    await page.emulateMedia({ colorScheme: 'light' })
    await page.addInitScript(() => {
      localStorage.setItem('stemtrace-theme', 'dark')
      // Read the attribute when the parser reaches <body>, before any module script runs.
      document.addEventListener('readystatechange', () => {
        if (document.readyState === 'interactive' && !('__themeAtParse' in window)) {
          ;(window as unknown as { __themeAtParse: string | undefined }).__themeAtParse =
            document.documentElement.dataset.theme
        }
      })
    })
    await page.goto('/executions')
    await expect
      .poll(() =>
        page.evaluate(() => (window as unknown as { __themeAtParse?: string }).__themeAtParse),
      )
      .toBe('dark')
  })
})
