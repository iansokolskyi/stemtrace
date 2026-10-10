import { expect, type Page, test } from '@playwright/test'

import { setupMockApi } from './fixtures/mock-api'

/**
 * Tests for 401 handling: an expired/missing session sends the user to login,
 * and a session signed by a different secret is explained instead.
 *
 * Mock mode only: the API is stubbed to always answer 401.
 */

const isRealMode = process.env.E2E_MODE === 'real'

const LOGIN_REDIRECT_KEY = 'stemtrace:login-redirect-at'

const mismatchNotice = (page: Page) =>
  page.getByRole('alert').filter({ hasText: 'signed by a different server process' })

const isApiRequest = (url: URL): boolean => url.pathname.startsWith('/api/')

async function mockUnauthorizedApi(page: Page, reason?: string): Promise<void> {
  const body: Record<string, string> = { detail: 'Authentication required' }
  if (reason) body.reason = reason
  await page.route(isApiRequest, (route) =>
    route.fulfill({
      status: 401,
      contentType: 'application/json',
      body: JSON.stringify(body),
    }),
  )
}

async function enableFormLogin(page: Page): Promise<void> {
  // The server injects the logout path only when built-in form login is enabled.
  await page.addInitScript(() => {
    window.__STEMTRACE_LOGOUT_PATH__ = '/logout'
  })
}

test.describe('Unauthorized API responses', () => {
  test.skip(isRealMode, 'Requires mocked 401 responses')

  test('redirects to login with next path when form login is enabled', async ({ page }) => {
    await enableFormLogin(page)
    await mockUnauthorizedApi(page)

    await page.goto('/workers')

    await expect(page).toHaveURL(/\/login\?next=%2Fworkers$/)
  })

  test('does not redirect when form login is not enabled', async ({ page }) => {
    await mockUnauthorizedApi(page)

    await page.goto('/workers')
    await page.waitForLoadState('networkidle')

    await expect(page).toHaveURL(/\/workers$/)
  })

  test('does not redirect again right after a redirect to login', async ({ page }) => {
    await enableFormLogin(page)
    await page.addInitScript((key) => {
      window.sessionStorage.setItem(key, String(Date.now()))
    }, LOGIN_REDIRECT_KEY)
    await mockUnauthorizedApi(page)

    await page.goto('/workers')
    await page.waitForLoadState('networkidle')

    await expect(page).toHaveURL(/\/workers$/)
    // A plain 401 is not a secret mismatch: no mismatch explanation.
    await expect(mismatchNotice(page)).toHaveCount(0)
  })

  test('explains a session signed by a different secret instead of redirecting', async ({
    page,
  }) => {
    await enableFormLogin(page)
    await mockUnauthorizedApi(page, 'session_secret_mismatch')

    await page.goto('/workers')
    await page.waitForLoadState('networkidle')

    await expect(page).toHaveURL(/\/workers$/)
    await expect(mismatchNotice(page)).toBeVisible()
    await expect(mismatchNotice(page)).toContainText('set STEMTRACE_LOGIN_SECRET to the same')
    await expect(page.getByRole('link', { name: 'Sign in again' })).toHaveAttribute(
      'href',
      '/login?next=%2Fworkers',
    )
    await expect(page.getByText('Failed to load workers')).toHaveCount(0)
  })

  test('mismatch notice offers sign-in and a reload recovers', async ({ page }) => {
    await enableFormLogin(page)
    await mockUnauthorizedApi(page, 'session_secret_mismatch')

    await page.goto('/workers')
    await expect(mismatchNotice(page)).toBeVisible()

    // Config fixed (or request landed on the right process): a full reload recovers.
    await page.unroute(isApiRequest)
    await setupMockApi(page)
    await page.reload()
    await expect(page.getByRole('heading', { name: 'Workers', level: 1 })).toBeVisible()
    await expect(mismatchNotice(page)).toHaveCount(0)
  })

  test('mismatch notice sign-in link opens the login page', async ({ page }) => {
    await enableFormLogin(page)
    await mockUnauthorizedApi(page, 'session_secret_mismatch')

    await page.goto('/workers')
    await page.getByRole('link', { name: 'Sign in again' }).click()

    await expect(page).toHaveURL(/\/login\?next=%2Fworkers$/)
  })
})
