import { expect, type Locator, type Page, test, type WebSocketRoute } from '@playwright/test'

import { setupMockApi } from './fixtures/mock-api'

/**
 * The Signal box frame: theme, sidebar, top strip, drawer, badges and live status.
 * Mock mode only (it controls storage, the OS colour scheme and the API).
 */

const isRealMode = process.env.E2E_MODE === 'real'

const DARK_BG = 'rgb(26, 28, 30)' // --bg #1A1C1E
const LIGHT_BG = 'rgb(239, 240, 241)' // --bg #EFF0F1

async function bodyBackground(page: Page): Promise<string> {
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

type Box = { x: number; y: number; width: number; height: number }

const overlaps = (a: Box, b: Box): boolean =>
  a.x < b.x + b.width && b.x < a.x + a.width && a.y < b.y + b.height && b.y < a.y + a.height

async function box(locator: Locator): Promise<Box> {
  const b = await locator.boundingBox()
  expect(b).not.toBeNull()
  return b as Box
}

async function horizontalOverflow(page: Page): Promise<number> {
  return page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  )
}

// The WebSocket fails at once, so the status reads "Polling" (the longest tooltip).
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

// A WebSocket that never connects: the status stays on "Connecting".
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

// Health (with version) succeeds, everything else is a session-secret mismatch: the frame
// shows the version and the longest status label, "Unauthorized".
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

const sidebar = (page: Page): Locator =>
  page.getByRole('complementary', { name: 'Primary navigation' })

/** Below 900px the sidebar is a drawer: open it so its contents can be checked. */
async function openSidebarIfDrawer(page: Page, width: number): Promise<void> {
  if (width >= 900) return
  const menu = page.getByRole('button', { name: 'Open navigation' })
  if ((await menu.getAttribute('aria-expanded')) !== 'true') await menu.click()
  // Wait for the slide-in to finish: a partly visible drawer passes plain toBeInViewport().
  await expect(sidebar(page)).toBeInViewport({ ratio: 1 })
}

test.describe('Sidebar', () => {
  test.skip(isRealMode, 'Mock-only test')

  test('nav lists the four pages in order and marks the current one', async ({ page }) => {
    await setupMockApi(page)
    await page.goto('/executions')
    const links = sidebar(page).getByRole('navigation', { name: 'Sections' }).getByRole('link')
    await expect(links).toHaveText([/^Executions/, /^Flows/, /^Workers/, /^Registry/])
    await expect(links.nth(0)).toHaveAttribute('aria-current', 'page')
    await links.nth(1).click()
    await expect(page).toHaveURL(/\/flows$/)
    await expect(links.nth(1)).toHaveAttribute('aria-current', 'page')
    await expect(links.nth(0)).not.toHaveAttribute('aria-current', 'page')
  })

  test('badges count offline workers and unregistered tasks without inspect', async ({ page }) => {
    const badgeRequests: string[] = []
    page.on('request', (r) => {
      const url = new URL(r.url())
      if (url.pathname === '/api/workers' || url.pathname === '/api/tasks/registry') {
        badgeRequests.push(url.pathname + url.search)
      }
    })
    await setupMockApi(page) // defaults: 1 offline worker, 1 not-registered task
    await page.goto('/flows')
    const nav = sidebar(page)
    await expect(nav.getByRole('link', { name: /Workers/ })).toContainText('1 offline')
    await expect(nav.getByRole('link', { name: /Registry/ })).toContainText('1 not registered')
    expect(badgeRequests).toContain('/api/workers?refresh=false')
    expect(badgeRequests).toContain('/api/tasks/registry?refresh=false')
    expect(badgeRequests.filter((r) => !r.includes('refresh=false'))).toEqual([])
  })

  for (const theme of ['light', 'dark'] as const) {
    test(`badges use the status tokens in ${theme}`, async ({ page }) => {
      await page.addInitScript((t) => localStorage.setItem('stemtrace-theme', t), theme)
      await setupMockApi(page)
      await page.goto('/flows')
      const nav = sidebar(page)
      const colours = async (link: RegExp, token: string) =>
        nav
          .getByRole('link', { name: link })
          .locator('span', { hasText: /offline|not registered/ })
          .evaluate((badge, name) => {
            // Resolve the token through a probe so both sides are rgb() strings
            const probe = document.createElement('div')
            probe.style.backgroundColor = `var(--${name})`
            probe.style.color = `var(--on-${name})`
            document.body.append(probe)
            const want = getComputedStyle(probe)
            const got = getComputedStyle(badge)
            const result = {
              bg: [got.backgroundColor, want.backgroundColor],
              fg: [got.color, want.color],
            }
            probe.remove()
            return result
          }, token)
      const offline = await colours(/Workers/, 'failure')
      expect(offline.bg[0]).toBe(offline.bg[1])
      expect(offline.fg[0]).toBe(offline.fg[1])
      const unregistered = await colours(/Registry/, 'anomaly')
      expect(unregistered.bg[0]).toBe(unregistered.bg[1])
      expect(unregistered.fg[0]).toBe(unregistered.fg[1])
    })
  }

  test('no badge when the count is zero or the request fails', async ({ page }) => {
    const api = await setupMockApi(page)
    api.setWorkers(api.workers.filter((w) => w.status === 'online'))
    await page.route(/\/api\/tasks\/registry/, (route) =>
      route.fulfill({ status: 500, body: '{}' }),
    )
    await page.goto('/flows')
    const nav = sidebar(page)
    await expect(nav.getByRole('link', { name: /Flows/ })).toHaveAttribute('aria-current', 'page')
    await expect(nav.getByRole('link', { name: /Workers/ })).toHaveText('Workers')
    await expect(nav.getByRole('link', { name: /Registry/ })).toHaveText('Registry')
  })

  test('live status is a static word', async ({ page }) => {
    await page.routeWebSocket(/\/ws$/, () => {})
    await setupMockApi(page)
    await page.goto('/executions')
    const status = page.getByTestId('live-status')
    await expect(status).toHaveText('Live')
    expect(await status.evaluate((el) => el.getAnimations({ subtree: true }).length)).toBe(0)
  })

  test('below 900px the sidebar is a drawer', async ({ page }) => {
    await page.setViewportSize({ width: 400, height: 800 })
    await setupMockApi(page)
    // Not /executions: the legacy task list's filter row overflows at 400px (a page body
    // that PR 2 replaces), which would mask the frame's own overflow.
    await page.goto('/workers')
    const menu = page.getByRole('button', { name: 'Open navigation' })
    await expect(sidebar(page)).not.toBeInViewport()
    expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0)

    await menu.click()
    await expect(sidebar(page)).toBeInViewport({ ratio: 1 })
    expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0)
    await page.keyboard.press('Escape')
    await expect(sidebar(page)).not.toBeInViewport()
    await expect(menu).toBeFocused()

    // Following a link closes the drawer too.
    await menu.click()
    await sidebar(page).getByRole('link', { name: /Flows/ }).click()
    await expect(page).toHaveURL(/\/flows$/)
    await expect(sidebar(page)).not.toBeInViewport()
  })

  test('no Google Fonts request and Overpass is loaded', async ({ page }) => {
    const fontCdn: string[] = []
    page.on('request', (r) => {
      if (/fonts\.(googleapis|gstatic)\.com/.test(r.url())) fontCdn.push(r.url())
    })
    await setupMockApi(page)
    await page.goto('/executions')
    await expect(page.getByTestId('live-status')).toBeVisible()
    const loaded = await page.evaluate(async () => {
      await document.fonts.ready
      return (
        document.fonts.check('13px "Overpass Variable"') &&
        [...document.fonts].some((f) => f.family.includes('Overpass') && f.status === 'loaded')
      )
    })
    expect(loaded).toBe(true)
    expect(fontCdn).toEqual([])
  })
})

// Migrated from header.spec.ts: the logo, nav, status and Logout now live in the sidebar.
test.describe('Sidebar footer and Logout', () => {
  test.skip(isRealMode, 'Requires injected form-login config')

  for (const width of [1280, 390, 360]) {
    test(`shows Logout in the sidebar without overlap at ${width}px`, async ({ page }) => {
      await page.setViewportSize({ width, height: 800 })
      await enableFormLogin(page)
      await failWebSocket(page)
      await setupMockApi(page)

      await page.goto('/workers')
      await openSidebarIfDrawer(page, width)

      const side = sidebar(page)
      const status = page.getByTestId('live-status')
      await expect(status).toHaveText('Polling')
      const logout = side.getByRole('button', { name: 'Logout' })
      await expect(logout).toBeVisible()
      await expect(side.locator('form')).toHaveAttribute('action', '/logout')
      await expect(side.locator('form')).toHaveAttribute('method', 'post')

      const parts = [
        await box(side.getByText('stemtrace', { exact: true })),
        await box(side.getByRole('navigation', { name: 'Sections' })),
        await box(status),
        await box(side.getByRole('button', { name: /Switch to/ })),
        await box(logout),
      ]
      for (let i = 0; i < parts.length; i++) {
        for (let j = i + 1; j < parts.length; j++) {
          expect(overlaps(parts[i], parts[j]), `sidebar parts ${i} and ${j} overlap`).toBe(false)
        }
      }
      for (const link of await side.getByRole('link').all()) {
        const b = await box(link)
        expect(b.x + b.width).toBeLessThanOrEqual(width)
      }
      // The status tooltip stays inside the viewport.
      await status.hover()
      const tooltip = page.getByText(/^Updates every 5s/)
      await expect(tooltip).toBeVisible()
      const tip = await box(tooltip)
      expect(tip.x).toBeGreaterThanOrEqual(0)
      expect(tip.x + tip.width).toBeLessThanOrEqual(width)

      expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0)
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

      const side = sidebar(page)
      await expect(page.getByTestId('live-status')).toHaveText(label)
      await expect(side.getByText('Version 0.1.0')).toBeVisible()
      const logout = side.getByRole('button', { name: 'Logout' })

      for (let width = 360; width <= 1280; width += 20) {
        await page.setViewportSize({ width, height: 800 })
        await openSidebarIfDrawer(page, width)
        expect(
          await horizontalOverflow(page),
          `horizontal overflow at ${width}px`,
        ).toBeLessThanOrEqual(0)
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

interface SocketCloseCounter {
  __socketCloses: number
}

// Migrated from header.spec.ts unchanged, apart from the status's new location.
test.describe('Live connection', () => {
  test.skip(isRealMode, 'Mock-only test')

  test('stays Live on one socket after the StrictMode remount', async ({ page }) => {
    const sockets: WebSocketRoute[] = []
    await page.routeWebSocket(/\/ws$/, (ws) => {
      sockets.push(ws)
    })
    // Count socket close events in the page. Registered after routeWebSocket so
    // it wraps Playwright's WebSocket mock rather than being replaced by it.
    await page.addInitScript(() => {
      const counter = window as unknown as SocketCloseCounter
      counter.__socketCloses = 0
      const MockedWebSocket = window.WebSocket
      window.WebSocket = class extends MockedWebSocket {
        constructor(url: string | URL, protocols?: string | string[]) {
          super(url, protocols)
          this.addEventListener('close', () => {
            counter.__socketCloses += 1
          })
        }
      }
    })
    const logs: string[] = []
    page.on('console', (message) => logs.push(message.text()))
    await setupMockApi(page)
    await page.clock.install()

    await page.goto('/')
    await expect(page.getByTestId('live-status')).toHaveText('Live')
    // StrictMode (dev) mounts twice: the first mount's socket is dropped. Wait
    // until its close event has reached the page, where a stale handler would
    // report "disconnected" and schedule a reconnect.
    await expect
      .poll(() => page.evaluate(() => (window as unknown as SocketCloseCounter).__socketCloses))
      .toBe(1)
    // Past the 3 s reconnect delay and the 5 s disconnected poll interval.
    await page.clock.runFor(6000)

    expect(logs.filter((line) => line.includes('[WebSocket] Disconnected'))).toEqual([])
    expect(sockets).toHaveLength(2)
    await expect(page.getByTestId('live-status')).toHaveText('Live')
  })
})
