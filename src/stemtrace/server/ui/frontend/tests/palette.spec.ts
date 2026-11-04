import { expect, type Locator, type Page, test } from '@playwright/test'

import { type MockApiContext, setupMockApi } from './fixtures/mock-api'
import {
  createFailedTask,
  createRegistryEntry,
  createSuccessTask,
  type MockTask,
  type MockWorker,
} from './fixtures/mock-data'

/**
 * The command palette (⌘K / Ctrl+K): keyboard-only paths to a task, a flow, a worker and a task
 * name, the roles it exposes, and the requests it makes. Mock mode only: it controls the API.
 * Text goes in with `page.keyboard`, as a person types it (never `fill`).
 */

test.skip(process.env.E2E_MODE === 'real', 'Mock-only test')

const minutesAgo = (minutes: number): string =>
  new Date(Date.now() - minutes * 60_000).toISOString()

function worker(hostname: string, status: MockWorker['status']): MockWorker {
  return {
    hostname,
    pid: 100,
    registered_tasks: ['shop.orders.reconcile'],
    status,
    registered_at: minutesAgo(120),
    last_seen: minutesAgo(status === 'online' ? 1 : 90),
  }
}

let reconcile: MockTask
let receipt: MockTask
let mockApi: MockApiContext

test.beforeEach(async ({ page }) => {
  mockApi = await setupMockApi(page, { useDefaults: false })
  reconcile = createSuccessTask('shop.orders.reconcile', 1)
  receipt = createFailedTask('shop.mail.send_receipt', 'SmtpError: refused', 'Traceback')
  mockApi.addTasks([reconcile, receipt])
  mockApi.setRegistry([
    createRegistryEntry('shop.orders.reconcile', 4),
    createRegistryEntry('shop.mail.send_receipt', 2),
    { ...createRegistryEntry('shop.legacy.purge', 0), status: 'not_registered' },
  ])
  mockApi.setWorkers([worker('celery@ingest-1', 'online'), worker('celery@ingest-2', 'offline')])
})

const dialog = (page: Page) => page.getByRole('dialog', { name: 'Search' })
const box = (page: Page) => dialog(page).getByRole('combobox')
const options = (page: Page) => dialog(page).getByRole('option')
const searchButton = (page: Page) => page.getByRole('button', { name: /^Search/ })

/** Loads a page and waits until the sidebar has its badge data, so the palette's sources are warm. */
async function loadPage(page: Page, path = '/executions'): Promise<void> {
  const registry = page.waitForResponse((r) => r.url().includes('/tasks/registry'))
  const workers = page.waitForResponse((r) => /\/api\/workers\?/.test(r.url()))
  await page.goto(path)
  await Promise.all([registry, workers])
}

async function openPalette(page: Page): Promise<void> {
  await page.keyboard.press('Control+k')
  await expect(box(page)).toBeFocused()
}

/** Arrow down until `target` is the selected option (bounded, so a regression fails, not hangs). */
async function arrowTo(page: Page, target: Locator): Promise<void> {
  await expect(target).toBeVisible()
  for (let i = 0; i < 12; i++) {
    if ((await target.getAttribute('aria-selected')) === 'true') return
    await page.keyboard.press('ArrowDown')
  }
  await expect(target).toHaveAttribute('aria-selected', 'true')
}

test.describe('opening and closing', () => {
  test('ctrl+k opens the dialog with the search box focused', async ({ page }) => {
    await loadPage(page)
    await expect(dialog(page)).toBeHidden()
    await page.keyboard.press('Control+k')
    await expect(dialog(page)).toBeVisible()
    await expect(box(page)).toBeFocused()
    await expect(box(page)).toHaveAttribute('aria-expanded', 'true')
  })

  test('meta+k opens the dialog too', async ({ page }) => {
    await loadPage(page)
    await page.keyboard.press('Meta+k')
    await expect(box(page)).toBeFocused()
  })

  test('escape closes the dialog and returns focus to the search button that opened it', async ({
    page,
  }) => {
    await loadPage(page)
    await searchButton(page).click()
    await expect(box(page)).toBeFocused()
    await page.keyboard.press('Escape')
    await expect(dialog(page)).toBeHidden()
    await expect(searchButton(page)).toBeFocused()
  })

  test('escape returns focus to the element focused before the shortcut', async ({ page }) => {
    await loadPage(page)
    const link = page.getByRole('link', { name: 'Workers' }).first()
    await link.focus()
    await openPalette(page)
    await page.keyboard.press('Escape')
    await expect(dialog(page)).toBeHidden()
    await expect(link).toBeFocused()
  })

  test('clicking outside the panel closes it and returns focus', async ({ page }) => {
    await loadPage(page)
    await searchButton(page).focus()
    await openPalette(page)
    await page.mouse.click(5, 5)
    await expect(dialog(page)).toBeHidden()
    await expect(searchButton(page)).toBeFocused()
  })

  test('the search button has an accessible name at 1280 and at 400 px', async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 800 })
    await loadPage(page)
    const label = searchButton(page).getByText('Search tasks, flows, workers')
    await expect(label).toBeVisible()
    await page.setViewportSize({ width: 400, height: 800 })
    await expect(searchButton(page)).toBeVisible()
    await expect(label).toBeHidden()
    await searchButton(page).click()
    await expect(box(page)).toBeFocused()
  })
})

test.describe('roles and keys', () => {
  test('exposes a combobox that controls a listbox, with the selected option active', async ({
    page,
  }) => {
    await loadPage(page)
    await openPalette(page)
    await page.keyboard.type('reconcile')
    const list = dialog(page).getByRole('listbox', { name: 'Results' })
    await expect(box(page)).toHaveAttribute('aria-controls', (await list.getAttribute('id')) ?? '')
    const first = options(page).first()
    await expect(first).toHaveAttribute('aria-selected', 'true')
    await expect(box(page)).toHaveAttribute(
      'aria-activedescendant',
      (await first.getAttribute('id')) ?? '',
    )
  })

  test('arrow keys move the selection and wrap around', async ({ page }) => {
    await loadPage(page)
    await openPalette(page)
    await page.keyboard.type('reconcile')
    await expect(options(page)).toHaveCount(2)
    await page.keyboard.press('ArrowDown')
    await expect(options(page).nth(1)).toHaveAttribute('aria-selected', 'true')
    await page.keyboard.press('ArrowDown')
    await expect(options(page).nth(0)).toHaveAttribute('aria-selected', 'true')
    await page.keyboard.press('ArrowUp')
    await expect(options(page).nth(1)).toHaveAttribute('aria-selected', 'true')
  })

  test('with no query it lists the flows and the workers', async ({ page }) => {
    await loadPage(page)
    await openPalette(page)
    await expect(dialog(page).getByRole('group', { name: 'Flows' })).toBeVisible()
    await expect(dialog(page).getByRole('group', { name: 'Workers' })).toBeVisible()
    await expect(options(page).filter({ hasText: 'celery@ingest-1' })).toHaveCount(1)
  })

  test('shows a muted "No results" line and Enter does nothing', async ({ page }) => {
    await loadPage(page)
    await openPalette(page)
    await page.keyboard.type('zzzqqq')
    await expect(dialog(page).getByRole('status')).toHaveText('No results for “zzzqqq”')
    await page.keyboard.press('Enter')
    await expect(dialog(page)).toBeVisible()
    await expect(page).toHaveURL(/\/executions/)
  })

  test('every option says its state in words, not colour alone', async ({ page }) => {
    await loadPage(page)
    await openPalette(page)
    await page.keyboard.type('ingest')
    await expect(options(page).filter({ hasText: 'celery@ingest-1' })).toContainText('Online')
    await expect(options(page).filter({ hasText: 'celery@ingest-2' })).toContainText('Offline')
    await page.keyboard.press('Control+a')
    await page.keyboard.type('purge')
    await expect(options(page).first()).toContainText('Not registered')
  })
})

test.describe('groups, drawer, errors and layouts', () => {
  test('every group is named by its heading, including headings of two words', async ({ page }) => {
    await loadPage(page)
    await openPalette(page)
    await page.keyboard.type('reconcile')
    await expect(dialog(page).getByRole('group', { name: 'Task names' })).toBeVisible()
    await expect(dialog(page).getByRole('group', { name: 'Flows' })).toBeVisible()
    await page.keyboard.press('Control+a')
    await page.keyboard.type(reconcile.task_id)
    await expect(dialog(page).getByRole('group', { name: 'Task IDs' })).toBeVisible()
  })

  test('escape closes the palette and leaves the open drawer open', async ({ page }) => {
    await page.setViewportSize({ width: 400, height: 800 })
    await loadPage(page)
    const menu = page.getByRole('button', { name: 'Open navigation' })
    await menu.click()
    await expect(menu).toHaveAttribute('aria-expanded', 'true')
    await page.keyboard.press('Control+k')
    await expect(box(page)).toBeFocused()
    await page.keyboard.press('Escape')
    await expect(dialog(page)).toBeHidden()
    await expect(menu).toHaveAttribute('aria-expanded', 'true')
    await expect(page.getByRole('navigation', { name: 'Sections' })).toBeVisible()
  })

  test('a source that fails to load is named, not reported as "No results"', async ({ page }) => {
    await page.route(/\/api\/tasks\/registry/, (route) =>
      route.fulfill({ status: 500, contentType: 'application/json', body: '{"detail":"boom"}' }),
    )
    await page.goto('/executions')
    await expect(page.getByRole('heading', { name: 'Executions', level: 1 })).toBeVisible()
    await openPalette(page)
    await page.keyboard.type('zzzqqq')
    await expect(dialog(page).getByRole('status')).toHaveText("Couldn't load task names")
  })

  test('the shortcut works on a layout whose key for K is not a Latin letter', async ({ page }) => {
    await loadPage(page)
    // Ukrainian layout: the physical K key types "л"
    await page.evaluate(() =>
      document.dispatchEvent(
        new KeyboardEvent('keydown', { key: 'л', code: 'KeyK', ctrlKey: true, bubbles: true }),
      ),
    )
    await expect(box(page)).toBeFocused()
  })

  test('the shortcut does not fire for another Latin letter on the physical K key', async ({
    page,
  }) => {
    await loadPage(page)
    // Dvorak: the physical K key types "v"
    await page.evaluate(() =>
      document.dispatchEvent(
        new KeyboardEvent('keydown', { key: 'v', code: 'KeyK', ctrlKey: true, bubbles: true }),
      ),
    )
    await expect(dialog(page)).toBeHidden()
  })
})

test.describe('keyboard-only paths', () => {
  test('a task ID opens that task', async ({ page }) => {
    await loadPage(page)
    await openPalette(page)
    await page.keyboard.type(receipt.task_id)
    const option = options(page).filter({ hasText: receipt.task_id })
    await expect(option).toContainText('send_receipt')
    await expect(option).toContainText('Failure')
    await page.keyboard.press('Enter')
    await expect(page).toHaveURL(new RegExp(`/tasks/${receipt.task_id}$`))
    await expect(dialog(page)).toBeHidden()
  })

  test('a task name opens Executions filtered to exactly that name', async ({ page }) => {
    await loadPage(page, '/workers')
    await openPalette(page)
    await page.keyboard.type('shop.mail.send_receipt')
    await expect(options(page).first()).toContainText('shop.mail.send_receipt')
    await page.keyboard.press('Enter')
    await expect(page).toHaveURL(/\/executions\?/)
    expect(new URL(page.url()).searchParams.get('name_exact')).toBe('shop.mail.send_receipt')
  })

  test('a flow opens its page', async ({ page }) => {
    await loadPage(page, '/workers')
    await openPalette(page)
    await page.keyboard.type('reconcile')
    await arrowTo(page, dialog(page).locator('[data-kind="flow"]'))
    await page.keyboard.press('Enter')
    await expect(page).toHaveURL(new RegExp(`/flows/${reconcile.task_id}$`))
  })

  test('a worker opens its page', async ({ page }) => {
    await loadPage(page)
    await openPalette(page)
    await page.keyboard.type('ingest-2')
    await arrowTo(page, dialog(page).locator('[data-kind="worker"]'))
    await page.keyboard.press('Enter')
    await expect(page).toHaveURL(/\/workers\/celery(%40|@)ingest-2$/)
  })
})

test.describe('lookups and failures', () => {
  test('Enter pressed before the task-ID lookup lands opens the task when it arrives', async ({
    page,
  }) => {
    await page.route(new RegExp(`/api/tasks/${receipt.task_id}$`), async (route) => {
      await new Promise((resolve) => setTimeout(resolve, 700))
      await route.fallback()
    })
    await loadPage(page)
    await openPalette(page)
    await page.keyboard.type(receipt.task_id)
    await page.keyboard.press('Enter')
    await expect(dialog(page)).toBeVisible()
    await expect(page).toHaveURL(new RegExp(`/tasks/${receipt.task_id}$`))
  })

  test('an Enter typed for an ID that turns out unknown does nothing', async ({ page }) => {
    await loadPage(page)
    await openPalette(page)
    await page.keyboard.type('deadbeef-0000')
    await page.keyboard.press('Enter')
    await expect(dialog(page).getByRole('status')).toHaveText('No results for “deadbeef-0000”')
    await expect(page).toHaveURL(/\/executions/)
  })

  test('an upper-case task ID finds the task', async ({ page }) => {
    await loadPage(page)
    await openPalette(page)
    await page.keyboard.type(receipt.task_id.toUpperCase())
    await expect(options(page).filter({ hasText: receipt.task_id })).toContainText('Failure')
  })

  test('a flows source that fails is named at once, without waiting out the retries', async ({
    page,
  }) => {
    await page.route(/\/api\/graphs/, (route) =>
      route.fulfill({ status: 500, contentType: 'application/json', body: '{"detail":"boom"}' }),
    )
    await page.goto('/executions')
    await expect(page.getByRole('heading', { name: 'Executions', level: 1 })).toBeVisible()
    await openPalette(page)
    await page.keyboard.type('zzzqqq')
    await expect(dialog(page).getByRole('status')).toHaveText("Couldn't load flows", {
      timeout: 2_000,
    })
  })

  test('failed sources read "a and b" and "a, b and c"', async ({ page }) => {
    const boom = { status: 500, contentType: 'application/json', body: '{"detail":"boom"}' }
    await page.route(/\/api\/graphs/, (route) => route.fulfill(boom))
    await page.route(/\/api\/tasks\/registry/, (route) => route.fulfill(boom))
    await page.goto('/executions')
    // The shortcut listener mounts with the frame: wait for the page before pressing
    await expect(page.getByRole('heading', { name: 'Executions', level: 1 })).toBeVisible()
    await openPalette(page)
    await page.keyboard.type('zzzqqq')
    await expect(dialog(page).getByRole('status')).toHaveText(
      "Couldn't load task names and flows",
      { timeout: 2_000 },
    )
  })

  test('three failed sources read "a, b and c"', async ({ page }) => {
    const boom = { status: 500, contentType: 'application/json', body: '{"detail":"boom"}' }
    await page.route(/\/api\/graphs/, (route) => route.fulfill(boom))
    await page.route(/\/api\/tasks\/registry/, (route) => route.fulfill(boom))
    await page.route(/\/api\/workers(\?|$)/, (route) => route.fulfill(boom))
    await page.goto('/executions')
    await expect(page.getByRole('heading', { name: 'Executions', level: 1 })).toBeVisible()
    await openPalette(page)
    await page.keyboard.type('zzzqqq')
    await expect(dialog(page).getByRole('status')).toHaveText(
      "Couldn't load task names, flows and workers",
      { timeout: 2_000 },
    )
  })

  test('a refresh that fails under a list already loaded is not a failed source', async ({
    page,
  }) => {
    await page.clock.install()
    await loadPage(page)
    await openPalette(page)
    await expect(options(page).first()).toBeVisible()
    await page.route(/\/api\/graphs/, (route) =>
      route.fulfill({ status: 500, contentType: 'application/json', body: '{"detail":"boom"}' }),
    )
    await page.clock.runFor(31_000)
    await page.keyboard.type('zzzqqq')
    await expect(dialog(page).getByRole('status')).toHaveText('No results for “zzzqqq”')
  })

  test('a cached task whose refetch fails keeps its row and says nothing is wrong', async ({
    page,
  }) => {
    await page.clock.install()
    await loadPage(page)
    await openPalette(page)
    await page.keyboard.type(receipt.task_id)
    await page.clock.runFor(300)
    await expect(options(page).filter({ hasText: receipt.task_id })).toBeVisible()
    // Leave the ID (the lookup is disabled), let the cached task go stale, come back to it
    await page.keyboard.press('Control+a')
    await page.keyboard.press('Backspace')
    await page.clock.runFor(6_000)
    await page.route(new RegExp(`/api/tasks/${receipt.task_id}$`), (route) =>
      route.fulfill({ status: 500, contentType: 'application/json', body: '{"detail":"boom"}' }),
    )
    const refetch = page.waitForResponse(
      (r) => r.url().endsWith(`/api/tasks/${receipt.task_id}`) && r.status() === 500,
    )
    await page.keyboard.type(receipt.task_id)
    await page.clock.runFor(300)
    await refetch
    // React renders the failed refetch a moment after the response (a real-time wait: the page
    // clock is faked), so an absent message means something only after it
    await new Promise((resolve) => setTimeout(resolve, 300))
    await expect(options(page).filter({ hasText: receipt.task_id })).toBeVisible()
    await expect(dialog(page).getByRole('status')).toHaveCount(0)
  })

  test('Enter on a row arrowed to opens that row even when the lookup finds a task', async ({
    page,
  }) => {
    const id = 'cafe0000-aaaa-bbbb'
    const found = createSuccessTask('shop.orders.found')
    found.task_id = id
    mockApi.addTasks([found])
    mockApi.setWorkers([worker(`celery@${id}-1`, 'online'), worker(`celery@${id}-2`, 'online')])
    await page.route(new RegExp(`/api/tasks/${id}$`), async (route) => {
      await new Promise((resolve) => setTimeout(resolve, 700))
      await route.fallback()
    })
    await loadPage(page)
    await openPalette(page)
    await page.keyboard.type(id)
    await expect(options(page)).toHaveCount(2)
    await page.keyboard.press('ArrowDown')
    await expect(options(page).nth(1)).toHaveAttribute('aria-selected', 'true')
    await page.keyboard.press('Enter')
    await expect(page).toHaveURL(new RegExp(`/workers/celery(%40|@)${id}-2$`))
  })

  test('arrow keys during a pending Enter do not move the highlight off the row it opens', async ({
    page,
  }) => {
    mockApi.setWorkers([
      worker('celery@deadbeef01', 'online'),
      worker('celery@deadbeef02', 'online'),
    ])
    await page.route(/\/api\/tasks\/deadbeef0$/, async (route) => {
      await new Promise((resolve) => setTimeout(resolve, 700))
      await route.fallback()
    })
    await loadPage(page)
    await openPalette(page)
    await page.keyboard.type('deadbeef0')
    await expect(options(page)).toHaveCount(2)
    await page.keyboard.press('Enter')
    await page.keyboard.press('ArrowDown')
    // The highlight and the action agree: row 1 is still selected, and it is the one that opens
    await expect(options(page).nth(0)).toHaveAttribute('aria-selected', 'true')
    await expect(page).toHaveURL(/\/workers\/celery(%40|@)deadbeef01$/)
  })

  test('Enter on a selected row opens it when the ID lookup finds nothing', async ({ page }) => {
    // A hex hostname has a task-ID shape: the lookup runs, 404s, and the worker row stays selected
    mockApi.setWorkers([worker('celery@3f4e5d6c7b8a', 'online')])
    await page.route(/\/api\/tasks\/3f4e5d6c7b8a$/, async (route) => {
      await new Promise((resolve) => setTimeout(resolve, 700))
      await route.fallback()
    })
    await loadPage(page)
    await openPalette(page)
    await page.keyboard.type('3f4e5d6c7b8a')
    await page.keyboard.press('Enter')
    await expect(page).toHaveURL(/\/workers\/celery(%40|@)3f4e5d6c7b8a$/)
  })

  test('a row arrowed to during the wait is the one Enter opens', async ({ page }) => {
    mockApi.setWorkers([
      worker('celery@deadbeef01', 'online'),
      worker('celery@deadbeef02', 'online'),
    ])
    await page.route(/\/api\/tasks\/deadbeef0$/, async (route) => {
      await new Promise((resolve) => setTimeout(resolve, 700))
      await route.fallback()
    })
    await loadPage(page)
    await openPalette(page)
    await page.keyboard.type('deadbeef0')
    await expect(options(page)).toHaveCount(2)
    await page.keyboard.press('ArrowDown')
    const second = options(page).nth(1)
    await expect(second).toHaveAttribute('aria-selected', 'true')
    const hostname = (await second.locator('span').first().innerText()).trim()
    await page.keyboard.press('Enter')
    await expect(page).toHaveURL(new RegExp(`/workers/${hostname.replace('@', '(%40|@)')}$`))
  })

  test('a task-ID lookup that fails with a server error says so, not "No results"', async ({
    page,
  }) => {
    await page.route(/\/api\/tasks\/deadbeef-0000$/, (route) =>
      route.fulfill({ status: 500, contentType: 'application/json', body: '{"detail":"boom"}' }),
    )
    await loadPage(page)
    await openPalette(page)
    await page.keyboard.type('deadbeef-0000')
    await expect(dialog(page).getByRole('status')).toHaveText("Couldn't load that task ID")
  })
})

test.describe('events and names', () => {
  test('the search button is named by its visible text', async ({ page }) => {
    await loadPage(page)
    await expect(page.getByRole('button', { name: 'Search tasks, flows, workers' })).toBeVisible()
  })

  test('a keydown without a key (browser autofill) does not throw', async ({ page }) => {
    const errors: string[] = []
    page.on('pageerror', (error) => errors.push(error.message))
    await loadPage(page)
    await page.evaluate(() => {
      const event = new KeyboardEvent('keydown', { ctrlKey: true, bubbles: true })
      Object.defineProperty(event, 'key', { value: undefined })
      document.dispatchEvent(event)
    })
    await page.keyboard.press('Control+k')
    await expect(box(page)).toBeFocused()
    expect(errors).toEqual([])
  })

  test('an Enter that commits a WebKit composition (keyCode 229) is left to the IME', async ({
    page,
  }) => {
    await loadPage(page)
    await openPalette(page)
    const prevented = await box(page).evaluate((input) => {
      const event = new KeyboardEvent('keydown', {
        key: 'Enter',
        keyCode: 229,
        bubbles: true,
        cancelable: true,
      })
      input.dispatchEvent(event)
      return event.defaultPrevented
    })
    expect(prevented).toBe(false)
  })

  test('an Enter that commits an IME composition is left to the IME', async ({ page }) => {
    await loadPage(page)
    await openPalette(page)
    const prevented = await box(page).evaluate((input) => {
      const event = new KeyboardEvent('keydown', {
        key: 'Enter',
        isComposing: true,
        bubbles: true,
        cancelable: true,
      })
      input.dispatchEvent(event)
      return event.defaultPrevented
    })
    expect(prevented).toBe(false)
  })
})

test.describe('requests', () => {
  test('opening and typing never asks Celery to inspect', async ({ page }) => {
    await loadPage(page)
    const seen: URL[] = []
    page.on('request', (request) => {
      const url = new URL(request.url())
      if (/\/api\/(workers|tasks\/registry)$/.test(url.pathname)) seen.push(url)
    })
    await openPalette(page)
    await page.keyboard.type('reconcile')
    await expect(options(page).first()).toBeVisible()
    await page.keyboard.press('Escape')
    // refresh=true is the absence of the parameter (the default), so every request must say false
    for (const url of seen) expect(url.searchParams.get('refresh'), url.href).toBe('false')
  })

  test('the names and workers come from the data the page already loaded', async ({ page }) => {
    await loadPage(page)
    const seen: string[] = []
    page.on('request', (request) => seen.push(new URL(request.url()).pathname))
    await openPalette(page)
    await page.keyboard.type('purge')
    await expect(options(page).first()).toContainText('shop.legacy.purge')
    expect(seen.filter((p) => /\/(workers|tasks\/registry)$/.test(p))).toEqual([])
  })

  test('only a task-ID-shaped query asks for a task, once, for the whole ID', async ({ page }) => {
    await loadPage(page)
    const lookups: string[] = []
    page.on('request', (request) => {
      const match = /\/api\/tasks\/([^/]+)$/.exec(new URL(request.url()).pathname)
      if (match && match[1] !== 'registry') lookups.push(match[1])
    })
    await openPalette(page)
    await page.keyboard.type('dead')
    await expect(dialog(page).getByRole('status')).toHaveText('No results for “dead”')
    // Longer than the lookup delay: a lookup, if one were sent, has been sent by now
    await page.waitForTimeout(600)
    expect(lookups).toEqual([])
    await page.keyboard.type('beef')
    await expect.poll(() => lookups).toEqual(['deadbeef'])
    await page.keyboard.press('Control+a')
    await page.keyboard.type(reconcile.task_id)
    await expect(options(page).filter({ hasText: reconcile.task_id })).toBeVisible()
    expect(lookups).toEqual(['deadbeef', reconcile.task_id])
  })
})
