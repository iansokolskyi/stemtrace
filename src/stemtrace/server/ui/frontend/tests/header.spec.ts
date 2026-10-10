import { expect, type Locator, type Page, test } from '@playwright/test'

import { setupMockApi } from './fixtures/mock-api'

/**
 * Header layout: the Logout control lives in the header (not a floating
 * overlay) and nothing overlaps or overflows, down to narrow phones.
 */

const isRealMode = process.env.E2E_MODE === 'real'

type Box = { x: number; y: number; width: number; height: number }

const overlaps = (a: Box, b: Box): boolean =>
  a.x < b.x + b.width && b.x < a.x + a.width && a.y < b.y + b.height && b.y < a.y + a.height

async function box(locator: Locator): Promise<Box> {
  const b = await locator.boundingBox()
  expect(b).not.toBeNull()
  return b as Box
}

// Make the WebSocket fail at once so the header shows "Polling", the status
// with the longest tooltip (worst case for overflow).
async function failWebSocket(page: Page): Promise<void> {
  await page.addInitScript(() => {
    class FailingWebSocket extends EventTarget {
      static readonly CONNECTING = 0
      static readonly OPEN = 1
      static readonly CLOSING = 2
      static readonly CLOSED = 3
      readyState = 3
      onopen: (() => void) | null = null
      onmessage: (() => void) | null = null
      onerror: (() => void) | null = null
      onclose: (() => void) | null = null
      constructor() {
        super()
        setTimeout(() => {
          this.onerror?.()
          this.onclose?.()
        }, 0)
      }
      close(): void {}
      send(): void {}
    }
    Object.defineProperty(window, 'WebSocket', { value: FailingWebSocket })
  })
}

// A WebSocket that never connects: the header stays on "Connecting".
async function pendingWebSocket(page: Page): Promise<void> {
  await page.addInitScript(() => {
    class PendingWebSocket extends EventTarget {
      static readonly CONNECTING = 0
      static readonly OPEN = 1
      static readonly CLOSING = 2
      static readonly CLOSED = 3
      readyState = 0
      onopen: (() => void) | null = null
      onmessage: (() => void) | null = null
      onerror: (() => void) | null = null
      onclose: (() => void) | null = null
      close(): void {}
      send(): void {}
    }
    Object.defineProperty(window, 'WebSocket', { value: PendingWebSocket })
  })
}

// Health (with version) succeeds, everything else is a session-secret mismatch:
// the header shows the version and the longest status label, "Unauthorized".
async function mismatchExceptHealth(page: Page): Promise<void> {
  await setupMockApi(page)
  await page.route(
    (url) => url.pathname.startsWith('/api/') && !url.pathname.startsWith('/api/health'),
    (route) =>
      route.fulfill({
        status: 401,
        contentType: 'application/json',
        body: JSON.stringify({
          detail: 'Authentication required',
          reason: 'session_secret_mismatch',
        }),
      }),
  )
}

async function enableFormLogin(page: Page): Promise<void> {
  await page.addInitScript(() => {
    window.__STEMTRACE_LOGOUT_PATH__ = '/logout'
  })
}

test.describe('Header', () => {
  test.skip(isRealMode, 'Requires injected form-login config')

  for (const width of [1280, 390, 360]) {
    test(`shows Logout in the header without overlap at ${width}px`, async ({ page }) => {
      await page.setViewportSize({ width, height: 800 })
      await enableFormLogin(page)
      await failWebSocket(page)
      await setupMockApi(page)

      await page.goto('/workers')

      const header = page.locator('header')
      const status = header.getByText('Polling')
      await expect(status).toBeVisible()
      const logout = header.getByRole('button', { name: 'Logout' })
      await expect(logout).toBeVisible()
      await expect(header.locator('form')).toHaveAttribute('action', '/logout')
      await expect(header.locator('form')).toHaveAttribute('method', 'post')

      const parts = [
        await box(header.getByRole('link', { name: /stemtrace/ })),
        await box(header.locator('nav')),
        await box(status),
        await box(logout),
      ]
      for (let i = 0; i < parts.length; i++) {
        for (let j = i + 1; j < parts.length; j++) {
          expect(overlaps(parts[i], parts[j]), `header parts ${i} and ${j} overlap`).toBe(false)
        }
      }
      for (const link of await header.locator('nav a').all()) {
        const b = await box(link)
        expect(b.x + b.width).toBeLessThanOrEqual(width)
      }
      // The status tooltip stays inside the viewport.
      await status.hover()
      const tooltip = header.getByText(/^Updates every 5s/)
      await expect(tooltip).toBeVisible()
      const tip = await box(tooltip)
      expect(tip.x).toBeGreaterThanOrEqual(0)
      expect(tip.x + tip.width).toBeLessThanOrEqual(width)

      const overflow = await page.evaluate(
        () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
      )
      expect(overflow).toBeLessThanOrEqual(0)
    })
  }

  for (const label of ['Unauthorized', 'Connecting']) {
    test(`no overflow and Logout fully visible from 360 to 1280px ("${label}")`, async ({
      page,
    }) => {
      await page.setViewportSize({ width: 1280, height: 800 })
      await enableFormLogin(page)
      await pendingWebSocket(page)
      if (label === 'Unauthorized') {
        await mismatchExceptHealth(page)
      } else {
        await setupMockApi(page)
      }

      await page.goto('/workers')

      const header = page.locator('header')
      await expect(header.getByText(label, { exact: true })).toBeVisible()
      await expect(header.getByText('v0.1.0')).toBeVisible()
      const logout = header.getByRole('button', { name: 'Logout' })

      for (let width = 360; width <= 1280; width += 20) {
        await page.setViewportSize({ width, height: 800 })
        const overflow = await page.evaluate(
          () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
        )
        expect(overflow, `horizontal overflow at ${width}px`).toBeLessThanOrEqual(0)
        const b = await box(logout)
        expect(b.x, `Logout cut off at ${width}px`).toBeGreaterThanOrEqual(0)
        expect(b.x + b.width, `Logout cut off at ${width}px`).toBeLessThanOrEqual(width)
      }
    })
  }

  test('has no Logout control when form login is disabled', async ({ page }) => {
    await setupMockApi(page)

    await page.goto('/workers')

    await expect(page.getByRole('heading', { name: 'Workers', level: 1 })).toBeVisible()
    await expect(page.getByRole('button', { name: 'Logout' })).toHaveCount(0)
  })
})
