import { expect, type Page, test } from '@playwright/test'

import { type MockApiContext, setupMockApi } from './fixtures/mock-api'
import {
  createRegistryEntry,
  createSuccessTask,
  type MockRegisteredTask,
} from './fixtures/mock-data'

/**
 * Tests for the Registry page: a table with task, group, status, runs and last run. Mocked API
 * responses, no Docker required; run with E2E_MODE=real for the Docker stack.
 */

const isRealMode = process.env.E2E_MODE === 'real'

const minutesAgo = (minutes: number): string =>
  new Date(Date.now() - minutes * 60_000).toISOString()

function entry(
  name: string,
  overrides: Partial<MockRegisteredTask> = {},
  runs = 1,
): MockRegisteredTask {
  return { ...createRegistryEntry(name, runs), task_group: 'jobs', ...overrides }
}

const active = (name: string, runs = 5, lastRun = minutesAgo(2)) =>
  entry(name, { last_run: lastRun }, runs)
const neverRun = (name: string, registeredBy = ['worker-1']) =>
  entry(name, { status: 'never_run', registered_by: registeredBy, last_run: null }, 0)
const unregistered = (name: string, runs = 3) =>
  entry(name, { status: 'not_registered', registered_by: [], last_run: minutesAgo(10) }, runs)

/**
 * Navigate and wait until the page has fetched the registry. The sidebar badge asks for the
 * same URL with `refresh=false`, so wait for the one without.
 */
async function loadRegistryPage(page: Page, navigate: () => Promise<unknown>): Promise<void> {
  const registryResponse = page.waitForResponse((response) => {
    const url = new URL(response.url())
    return /\/api\/tasks\/registry$/.test(url.pathname) && !url.searchParams.has('refresh')
  })
  await navigate()
  await registryResponse
}

const table = (page: Page) => page.getByRole('table', { name: 'Registry' })
const rowOf = (page: Page, name: string) =>
  table(page)
    .getByRole('row')
    .filter({ has: page.locator(`[title="${name}"]`) })

/** A mixed registry: active, never run, and not registered. */
function mixed(): MockRegisteredTask[] {
  return [
    active('app.jobs.add', 1204, minutesAgo(2)),
    neverRun('app.jobs.report', ['worker-1', 'worker-2']),
    unregistered('app.jobs.old_sync'),
  ]
}

test.describe('Registry page', () => {
  let mockApi: MockApiContext | null = null

  test.beforeEach(async ({ page }) => {
    mockApi = isRealMode ? null : await setupMockApi(page, { useDefaults: false })
  })

  test('shows the heading and how many task names are known', async ({ page }) => {
    mockApi?.setRegistry(mixed())
    await loadRegistryPage(page, () => page.goto('/registry'))
    await expect(page.getByRole('heading', { name: 'Registry', level: 1 })).toBeVisible()
    if (isRealMode) {
      await expect(
        page.getByText(/^\d+ task names? known to workers or seen on the broker$/),
      ).toBeVisible()
      return
    }
    await expect(
      page.getByText('3 task names known to workers or seen on the broker'),
    ).toBeVisible()
  })

  test('says "1 task name" for a single task', async ({ page }) => {
    test.skip(isRealMode, 'Mock-only test')
    mockApi?.setRegistry([active('app.jobs.add')])
    await loadRegistryPage(page, () => page.goto('/registry'))
    await expect(page.getByText('1 task name known to workers or seen on the broker')).toBeVisible()
  })

  test('lists one row per task with name, group, status, runs and last run', async ({ page }) => {
    mockApi?.setRegistry(mixed())
    await loadRegistryPage(page, () => page.goto('/registry'))
    if (isRealMode) {
      // Real Docker mode: the backend's tasks, never the empty state
      await expect(table(page).getByRole('row')).not.toHaveCount(1)
      await expect(page.getByTestId('registry-status').first()).toBeVisible()
      await expect(page.getByText('No tasks discovered yet')).toHaveCount(0)
      return
    }

    await expect(
      table(page)
        .getByRole('columnheader')
        .evaluateAll((cells) => cells.map((cell) => cell.textContent)),
    ).resolves.toEqual(['Task', 'Group', 'Status', 'Runs', 'Last run'])

    const add = rowOf(page, 'app.jobs.add').getByRole('cell')
    await expect(add.nth(0)).toHaveText('app.jobs.add')
    await expect(add.nth(1)).toHaveText('jobs')
    await expect(add.nth(2)).toHaveText('Active')
    await expect(add.nth(3)).toHaveText('1,204')
    await expect(add.nth(4)).toHaveText('2m ago')

    const report = rowOf(page, 'app.jobs.report').getByRole('cell')
    await expect(report.nth(2)).toHaveText('Never run')
    await expect(report.nth(3)).toHaveText('0')
    await expect(report.nth(4)).toHaveText('Never')

    const old = rowOf(page, 'app.jobs.old_sync').getByRole('cell')
    await expect(old.nth(2)).toHaveText('Not registered')
    await expect(old.nth(3)).toHaveText('3')
    await expect(old.nth(4)).toHaveText('10m ago')
  })

  test('a task name shows its module muted and the task in semibold, full name on hover', async ({
    page,
  }) => {
    test.skip(isRealMode, 'Mock-only test')
    mockApi?.setRegistry([active('app.billing.send'), active('app.email.send'), active('add')])
    await loadRegistryPage(page, () => page.goto('/registry'))
    // Names are never shortened: two tasks called `send` stay apart by their full names
    for (const name of ['app.billing.send', 'app.email.send']) {
      const label = page.locator(`[title="${name}"]`)
      await expect(label).toHaveText(name)
      await expect(label.locator('[data-task-module]')).toHaveText(name.replace(/send$/, ''))
      await expect(label.locator('[data-task-last]')).toHaveText('send')
    }
    await expect(page.locator('[title="add"] [data-task-module]')).toHaveCount(0)
  })

  test('only a task no worker registers is loud; the others are quiet', async ({ page }) => {
    test.skip(isRealMode, 'Mock-only test')
    mockApi?.setRegistry(mixed())
    await loadRegistryPage(page, () => page.goto('/registry'))
    const status = (name: string) => rowOf(page, name).getByTestId('registry-status')
    await expect(status('app.jobs.add')).toHaveAttribute('data-variant', 'quiet')
    await expect(status('app.jobs.report')).toHaveAttribute('data-variant', 'quiet')
    await expect(status('app.jobs.old_sync')).toHaveAttribute('data-variant', 'loud')
    await expect(status('app.jobs.old_sync')).toHaveAttribute('data-status', 'not_registered')
    // Status is never colour alone: an icon and the word
    for (const name of ['app.jobs.add', 'app.jobs.report', 'app.jobs.old_sync']) {
      await expect(status(name).locator('svg')).toHaveCount(1)
    }
    // Only that row is tinted as a problem
    await expect(rowOf(page, 'app.jobs.old_sync')).toHaveAttribute('data-problem', 'true')
    await expect(rowOf(page, 'app.jobs.add')).not.toHaveAttribute('data-problem', 'true')
    await expect(rowOf(page, 'app.jobs.report')).not.toHaveAttribute('data-problem', 'true')
  })

  test('each status is explained under the name, and an active task has no note', async ({
    page,
  }) => {
    test.skip(isRealMode, 'Mock-only test')
    mockApi?.setRegistry(mixed())
    await loadRegistryPage(page, () => page.goto('/registry'))
    const note = (name: string) => rowOf(page, name).getByTestId('registry-note')
    await expect(note('app.jobs.old_sync')).toHaveText(
      'No worker has this task registered. New runs with this name may stay in Pending.',
    )
    await expect(note('app.jobs.report')).toHaveText(
      'Registered by worker-1, worker-2; it has not run yet.',
    )
    await expect(note('app.jobs.add')).toHaveCount(0)
  })

  test('a row opens Executions filtered to that exact name, from any cell', async ({ page }) => {
    test.skip(isRealMode, 'Mock-only test')
    mockApi?.setRegistry([active('app.add'), active('app.add_numbers')])
    mockApi?.addTasks([
      createSuccessTask('app.add'),
      createSuccessTask('app.add_numbers'),
      createSuccessTask('other.app.add'),
    ])
    await loadRegistryPage(page, () => page.goto('/registry'))
    await expect(page.getByRole('link', { name: 'app.add', exact: true })).toHaveAttribute(
      'href',
      '/executions?name_exact=app.add',
    )
    // The link's overlay covers the row: click a cell that is not the link's text
    await rowOf(page, 'app.add').getByRole('cell').nth(3).click({ force: true })
    await expect(page).toHaveURL(/\/executions\?name_exact=app\.add$/)

    // The runs of that exact task, not of tasks whose names extend it or contain it
    const rows = page.locator('a[href^="/tasks/"]')
    await expect(rows).toHaveCount(1)
    await expect(rows.first().locator('[title="app.add"]')).toHaveText('app.add')
    await expect(page.getByText('app.add_numbers')).toHaveCount(0)
    await expect(page.getByText('other.app.add')).toHaveCount(0)
  })

  test('a never-run and a not-registered row open Executions too', async ({ page }) => {
    test.skip(isRealMode, 'Mock-only test')
    mockApi?.setRegistry(mixed())
    await loadRegistryPage(page, () => page.goto('/registry'))
    for (const name of ['app.jobs.report', 'app.jobs.old_sync']) {
      await expect(page.getByRole('link', { name, exact: true })).toHaveAttribute(
        'href',
        `/executions?name_exact=${name}`,
      )
    }
  })

  test('the link carries the range Executions last showed', async ({ page }) => {
    test.skip(isRealMode, 'Mock-only test')
    mockApi?.setRegistry([active('app.jobs.add')])
    await page.goto('/executions?range=1h')
    await expect(page.getByRole('heading', { name: 'Executions', level: 1 })).toBeVisible()
    await page
      .getByRole('navigation', { name: 'Sections' })
      .getByRole('link', { name: /^Registry/ })
      .click()
    await expect(page.getByRole('link', { name: 'app.jobs.add', exact: true })).toHaveAttribute(
      'href',
      '/executions?name_exact=app.jobs.add&range=1h',
    )
  })

  test('shows no column for what the API does not send', async ({ page }) => {
    test.skip(isRealMode, 'Mock-only test')
    mockApi?.setRegistry(mixed())
    await loadRegistryPage(page, () => page.goto('/registry'))
    for (const absent of ['Queue', 'Fail %', 'p50', 'p95', 'Runs (6 h)']) {
      await expect(table(page).getByRole('columnheader', { name: absent })).toHaveCount(0)
    }
    // No placeholder dash stands in for them either
    await expect(table(page).getByText('—')).toHaveCount(0)
  })

  test('the Group column appears only when the server sends groups', async ({ page }) => {
    test.skip(isRealMode, 'Mock-only test')
    const { task_group: _dropped, ...older } = active('app.jobs.add')
    mockApi?.setRegistry([older])
    await loadRegistryPage(page, () => page.goto('/registry'))
    await expect(table(page).getByRole('columnheader', { name: 'Status' })).toBeVisible()
    await expect(table(page).getByRole('columnheader', { name: 'Group' })).toHaveCount(0)

    mockApi?.setRegistry([active('app.jobs.add')])
    await loadRegistryPage(page, () => page.reload())
    await expect(table(page).getByRole('columnheader', { name: 'Group' })).toBeVisible()
  })

  test('the page asks for a refreshed registry and the badge for the stored one', async ({
    page,
  }) => {
    test.skip(isRealMode, 'Mock-only test')
    const requests: string[] = []
    page.on('request', (request) => {
      const url = new URL(request.url())
      if (/\/api\/tasks\/registry/.test(url.pathname)) requests.push(url.pathname + url.search)
    })
    mockApi?.setRegistry(mixed())
    await loadRegistryPage(page, () => page.goto('/registry'))
    // The badge's request lands after the page's, wait for it so the log is complete
    await expect.poll(() => requests.includes('/api/tasks/registry?refresh=false')).toBe(true)
    expect(requests.filter((r) => r === '/api/tasks/registry')).toHaveLength(1)
    expect(requests.filter((r) => r === '/api/tasks/registry?refresh=false')).toHaveLength(1)
  })

  test('the sidebar badge counts the not-registered tasks and matches the rows', async ({
    page,
  }) => {
    test.skip(isRealMode, 'Mock-only test')
    mockApi?.setRegistry([unregistered('a.one'), unregistered('a.two'), active('a.three')])
    await loadRegistryPage(page, () => page.goto('/registry'))
    const link = page
      .getByRole('navigation', { name: 'Sections' })
      .getByRole('link', { name: /Registry/ })
    await expect(link).toContainText('2 not registered')
    await expect(
      page.locator('[data-testid="registry-status"][data-status="not_registered"]'),
    ).toHaveCount(2)
  })

  test('the sidebar shows no badge when every task is registered', async ({ page }) => {
    test.skip(isRealMode, 'Mock-only test')
    mockApi?.setRegistry([active('a.one'), neverRun('a.two')])
    await loadRegistryPage(page, () => page.goto('/registry'))
    const link = page
      .getByRole('navigation', { name: 'Sections' })
      .getByRole('link', { name: /Registry/ })
    await expect(link).toBeVisible()
    await expect(link).not.toContainText('not registered')
  })

  test.describe('search', () => {
    test.skip(isRealMode, 'Mock-only test')

    test('filters by name, group and status word, in the browser', async ({ page }) => {
      mockApi?.setRegistry([
        active('app.jobs.add'),
        entry('app.mail.send', { last_run: minutesAgo(5), task_group: 'comms' }),
        unregistered('app.jobs.old_sync'),
        neverRun('app.jobs.report'),
      ])
      const requests: string[] = []
      page.on('request', (request) => {
        if (/\/api\/tasks\/registry/.test(request.url())) requests.push(request.url())
      })
      await loadRegistryPage(page, () => page.goto('/registry'))
      await expect.poll(() => requests.length).toBe(2)
      const search = page.getByRole('searchbox', { name: 'Search the registry' })
      const names = () =>
        table(page)
          .getByRole('row')
          .locator('[title]')
          .evaluateAll((els) => els.map((el) => el.getAttribute('title')))

      await search.fill('add')
      await expect.poll(names).toEqual(['app.jobs.add'])
      await search.fill('comms')
      await expect.poll(names).toEqual(['app.mail.send'])
      await search.fill('not registered')
      await expect.poll(names).toEqual(['app.jobs.old_sync'])
      await search.fill('never run')
      await expect.poll(names).toEqual(['app.jobs.report'])
      await search.fill('JOBS Active')
      await expect.poll(names).toEqual(['app.jobs.add'])
      await search.fill('')
      await expect.poll(names).toHaveLength(4)
      // Searching never asked the server again
      expect(requests).toHaveLength(2)
    })

    test('says so when nothing matches', async ({ page }) => {
      mockApi?.setRegistry(mixed())
      await loadRegistryPage(page, () => page.goto('/registry'))
      await page.getByRole('searchbox', { name: 'Search the registry' }).fill('zzz')
      await expect(page.getByText('No tasks match “zzz”.')).toBeVisible()
      await expect(table(page)).toHaveCount(0)
    })
  })

  test('shows an empty state when no tasks are known', async ({ page }) => {
    test.skip(isRealMode, 'Mock-only test')
    await loadRegistryPage(page, () => page.goto('/registry'))
    await expect(page.getByText('No tasks discovered yet')).toBeVisible()
    await expect(table(page)).toHaveCount(0)
    await expect(page.getByRole('searchbox')).toHaveCount(0)
  })

  test('says so when the registry cannot be loaded', async ({ page }) => {
    test.skip(isRealMode, 'Mock-only test')
    await page.route(/\/api\/tasks\/registry$/, (route) =>
      route.fulfill({ status: 500, body: 'boom' }),
    )
    await page.goto('/registry')
    await expect(page.getByText('Failed to load task registry')).toBeVisible({ timeout: 15_000 })
  })

  for (const theme of ['light', 'dark'] as const) {
    test(`the not-registered chip uses the anomaly tokens in ${theme}`, async ({ page }) => {
      test.skip(isRealMode, 'Mock-only test')
      await page.addInitScript((t) => localStorage.setItem('stemtrace-theme', t), theme)
      mockApi?.setRegistry(mixed())
      await loadRegistryPage(page, () => page.goto('/registry'))
      const block = rowOf(page, 'app.jobs.old_sync')
        .getByTestId('registry-status')
        .locator('[data-status-icon]')
      const got = await block.evaluate((el) => {
        const probe = document.createElement('div')
        probe.style.backgroundColor = 'var(--anomaly)'
        document.body.append(probe)
        const want = getComputedStyle(probe).backgroundColor
        probe.remove()
        return { actual: getComputedStyle(el).backgroundColor, want }
      })
      expect(got.actual).toBe(got.want)
    })
  }

  test('at 400px the page does not scroll sideways', async ({ page }) => {
    test.skip(isRealMode, 'Mock-only test')
    mockApi?.setRegistry(mixed())
    await page.setViewportSize({ width: 400, height: 800 })
    await loadRegistryPage(page, () => page.goto('/registry'))
    await expect(rowOf(page, 'app.jobs.add')).toBeVisible()
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(400)
  })
})
