import { expect, type Page, test } from '@playwright/test'

import { type MockApiContext, setupMockApi } from './fixtures/mock-api'
import {
  createRegistryEntry,
  createSuccessTask,
  createTaskStats,
  type MockRegisteredTask,
} from './fixtures/mock-data'
import { columnHeaders, expectFilledWithToken } from './fixtures/table'

/**
 * Tests for the Registry page: a table with task, group, status, 6 h stats and last run. Mocked API
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

/**
 * Stats for the mixed registry's `app.jobs.add`: 12 runs in the window, 2 failed, 12 runtime
 * samples. Nearest rank: p50 is the 6th sample (1.2 s), p95 the 12th (125 s).
 */
const ADD_RUNTIMES = [100, 200, 300, 400, 500, 1200, 1300, 1400, 2000, 3000, 45_000, 125_000]
const addStats = () => createTaskStats('app.jobs.add', 12, 2, ADD_RUNTIMES)

const STATS_COLUMNS = { runs: 3, fail: 4, p50: 5, p95: 6, last: 7 } as const

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

  test('lists one row per task with name, group, status, 6 h stats and last run', async ({
    page,
  }) => {
    mockApi?.setRegistry(mixed())
    mockApi?.setTaskStats([addStats(), createTaskStats('app.jobs.old_sync', 3, 0, [250])])
    await loadRegistryPage(page, () => page.goto('/registry'))
    if (isRealMode) {
      // Real Docker mode: the backend's tasks, never the empty state
      await expect(table(page).getByRole('row')).not.toHaveCount(1)
      await expect(page.getByTestId('registry-status').first()).toBeVisible()
      await expect(page.getByText('No tasks discovered yet')).toHaveCount(0)
      return
    }

    expect(await columnHeaders(table(page))).toEqual([
      'Task',
      'Group',
      'Status',
      'Runs (6 h)',
      'Fail %',
      'p50',
      'p95',
      'Last run',
    ])

    const add = rowOf(page, 'app.jobs.add').getByRole('cell')
    await expect(add.nth(0)).toHaveText('app.jobs.add')
    await expect(add.nth(1)).toHaveText('jobs')
    await expect(add.nth(2)).toHaveText('Active')
    await expect(add.nth(STATS_COLUMNS.runs)).toHaveText('12')
    await expect(add.nth(STATS_COLUMNS.fail)).toHaveText('17%')
    await expect(add.nth(STATS_COLUMNS.p50)).toHaveText('1.2s')
    await expect(add.nth(STATS_COLUMNS.p95)).toHaveText('2m 5s')
    // Last run is the registry's own: the lifetime count (1,204) is not what Runs shows
    await expect(add.nth(STATS_COLUMNS.last)).toHaveText('2m ago')

    const old = rowOf(page, 'app.jobs.old_sync').getByRole('cell')
    await expect(old.nth(2)).toHaveText('Not registered')
    await expect(old.nth(STATS_COLUMNS.runs)).toHaveText('3')
    await expect(old.nth(STATS_COLUMNS.fail)).toHaveText('0%')
    await expect(old.nth(STATS_COLUMNS.p50)).toHaveText('250ms')
    await expect(old.nth(STATS_COLUMNS.p95)).toHaveText('250ms')
    await expect(old.nth(STATS_COLUMNS.last)).toHaveText('10m ago')
  })

  test('the stats request asks for the last 6 hours, once', async ({ page }) => {
    test.skip(isRealMode, 'Mock-only test')
    mockApi?.setRegistry(mixed())
    await loadRegistryPage(page, () => page.goto('/registry'))
    await expect.poll(() => mockApi?.taskStatsRequests.length).toBe(1)
    expect(mockApi?.taskStatsRequests[0].search).toBe('?within=6h')
  })

  test('a name with no runs in the window shows 0, no rate and dashes, keeping its last run', async ({
    page,
  }) => {
    test.skip(isRealMode, 'Mock-only test')
    mockApi?.setRegistry([active('app.jobs.add', 40, minutesAgo(420)), neverRun('app.jobs.report')])
    // The server's row for a name whose runs are all older than the 6 h window: 0 runs, last run 7 h ago
    mockApi?.setTaskStats([createTaskStats('app.jobs.add', 0, 0)])
    await loadRegistryPage(page, () => page.goto('/registry'))
    for (const name of ['app.jobs.add', 'app.jobs.report']) {
      const cells = rowOf(page, name).getByRole('cell')
      await expect(cells.nth(STATS_COLUMNS.runs)).toHaveText('0')
      await expect(cells.nth(STATS_COLUMNS.fail)).toHaveText('')
      await expect(cells.nth(STATS_COLUMNS.p50)).toHaveText('—')
      await expect(cells.nth(STATS_COLUMNS.p95)).toHaveText('—')
    }
    await expect(rowOf(page, 'app.jobs.add').getByRole('cell').nth(STATS_COLUMNS.last)).toHaveText(
      '7h ago',
    )
    await expect(
      rowOf(page, 'app.jobs.report').getByRole('cell').nth(STATS_COLUMNS.last),
    ).toHaveText('Never')
  })

  test.describe('refresh with the socket connected', () => {
    test.skip(isRealMode, 'Mock-only test')

    const POLL_MS = 30_000
    const ROW = 'app.jobs.report'

    /** The row's cells as text: Task (with its note), Group, Status, Runs, Fail %, p50, p95, Last run. */
    const cellsOf = (page: Page, name: string) =>
      rowOf(page, name)
        .getByRole('cell')
        .evaluateAll((cells) => cells.map((cell) => cell.textContent))

    test.beforeEach(async ({ page }) => {
      await page.routeWebSocket(/\/ws$/, () => {})
      await page.clock.install()
    })

    test('a task that starts running changes status, note, runs and last run in one step', async ({
      page,
    }) => {
      mockApi?.setRegistry([neverRun(ROW)])
      mockApi?.setTaskStats([])
      await loadRegistryPage(page, () => page.goto('/registry'))
      const before = [
        `${ROW}Registered by worker-1; it has not run yet.`,
        'jobs',
        'Never run',
        '0',
        '',
        '—',
        '—',
        'Never',
      ]
      // evaluateAll does not wait for the row: poll until the first snapshot is rendered
      await expect.poll(() => cellsOf(page, ROW)).toEqual(before)

      // From here on, record every state the row passes through
      await page.evaluate((name) => {
        const seen: (string | null)[][] = []
        ;(window as unknown as { __rows: typeof seen }).__rows = seen
        new MutationObserver(() => {
          const row = [...document.querySelectorAll('tr')].find((tr) =>
            tr.querySelector(`[title="${name}"]`),
          )
          if (row) seen.push([...row.querySelectorAll('td')].map((td) => td.textContent))
        }).observe(document.body, { subtree: true, childList: true, characterData: true })
      }, ROW)

      mockApi?.setRegistry([active(ROW, 3, minutesAgo(1))])
      mockApi?.setTaskStats([createTaskStats(ROW, 3, 1, [400, 500, 600])])
      await page.clock.runFor(POLL_MS + 1000)

      const after = [ROW, 'jobs', 'Active', '3', '33%', '500ms', '600ms', '1m ago']
      await expect.poll(() => cellsOf(page, ROW)).toEqual(after)
      // No intermediate state mixes the old and the new: each is wholly one or the other
      const seen = await page.evaluate(
        () => (window as unknown as { __rows: (string | null)[][] }).__rows,
      )
      for (const state of seen) expect([before, after]).toContainEqual(state)
    })

    test('a stats request that fails on a later poll empties the stats cells', async ({ page }) => {
      mockApi?.setRegistry([active(ROW, 3, minutesAgo(1))])
      mockApi?.setTaskStats([createTaskStats(ROW, 3, 1, [400, 500, 600])])
      await loadRegistryPage(page, () => page.goto('/registry'))
      await expect(rowOf(page, ROW).getByRole('cell').nth(STATS_COLUMNS.runs)).toHaveText('3')

      mockApi?.setTaskStats('error')
      await page.clock.runFor(POLL_MS + 1000)
      const cells = rowOf(page, ROW).getByRole('cell')
      for (const column of ['runs', 'fail', 'p50', 'p95'] as const) {
        await expect(cells.nth(STATS_COLUMNS[column])).toHaveText('')
      }
      await expect(cells.nth(2)).toHaveText('Active')
      await expect(cells.nth(STATS_COLUMNS.last)).toHaveText('1m ago')
    })

    test('polls read the stored registry; only the first load asks Celery', async ({ page }) => {
      const requests: string[] = []
      page.on('request', (request) => {
        const url = new URL(request.url())
        if (url.pathname === '/api/tasks/registry') requests.push(url.search)
      })
      mockApi?.setRegistry(mixed())
      await loadRegistryPage(page, () => page.goto('/registry'))
      await expect.poll(() => requests.length).toBe(2) // the page's, then the sidebar badge's
      await page.clock.runFor(POLL_MS + 1000)
      await expect.poll(() => requests.length).toBe(4) // the page's poll, the badge's poll
      expect(requests.filter((search) => search === '')).toHaveLength(1)
    })
  })

  test('stats join by exact name: a longer name never lends its numbers', async ({ page }) => {
    test.skip(isRealMode, 'Mock-only test')
    mockApi?.setRegistry([active('app.add'), active('app.add_numbers')])
    mockApi?.setTaskStats([createTaskStats('app.add_numbers', 9, 3, [500])])
    await loadRegistryPage(page, () => page.goto('/registry'))
    await expect(
      rowOf(page, 'app.add_numbers').getByRole('cell').nth(STATS_COLUMNS.runs),
    ).toHaveText('9')
    await expect(rowOf(page, 'app.add').getByRole('cell').nth(STATS_COLUMNS.runs)).toHaveText('0')
  })

  test('a failure rate above 1% is bold and in the failure colour; 1% and under is not', async ({
    page,
  }) => {
    test.skip(isRealMode, 'Mock-only test')
    mockApi?.setRegistry([active('a.high'), active('a.edge'), active('a.none')])
    mockApi?.setTaskStats([
      createTaskStats('a.high', 100, 2, [10]),
      createTaskStats('a.edge', 100, 1, [10]),
      createTaskStats('a.none', 100, 0, [10]),
    ])
    await loadRegistryPage(page, () => page.goto('/registry'))
    const fail = (name: string) => rowOf(page, name).getByRole('cell').nth(STATS_COLUMNS.fail)
    await expect(fail('a.high')).toHaveText('2.0%')
    await expect(fail('a.high')).toHaveCSS('font-weight', '700')
    await expect(fail('a.edge')).toHaveText('1.0%')
    await expect(fail('a.edge')).not.toHaveCSS('font-weight', '700')
    await expect(fail('a.none')).toHaveText('0%')
    await expect(fail('a.none')).not.toHaveCSS('font-weight', '700')
  })

  test('when the stats request fails the table still shows, with empty stats cells', async ({
    page,
  }) => {
    test.skip(isRealMode, 'Mock-only test')
    mockApi?.setRegistry(mixed())
    mockApi?.setTaskStats('error')
    await loadRegistryPage(page, () => page.goto('/registry'))
    await expect.poll(() => mockApi?.taskStatsRequests.length).toBe(1)
    await expect(page.getByText('Failed to load task registry')).toHaveCount(0)
    expect(await table(page).getByRole('row').count()).toBe(4)
    const add = rowOf(page, 'app.jobs.add').getByRole('cell')
    await expect(add.nth(2)).toHaveText('Active')
    for (const column of ['runs', 'fail', 'p50', 'p95'] as const) {
      await expect(add.nth(STATS_COLUMNS[column])).toHaveText('')
    }
    await expect(add.nth(STATS_COLUMNS.last)).toHaveText('2m ago')
    // No made-up zero for a name whose stats are unknown
    await expect(table(page).getByText('0', { exact: true })).toHaveCount(0)
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

  test('shows no Queue column: the registry API does not send a queue per name', async ({
    page,
  }) => {
    test.skip(isRealMode, 'Mock-only test')
    mockApi?.setRegistry(mixed())
    await loadRegistryPage(page, () => page.goto('/registry'))
    await expect(table(page).getByRole('columnheader', { name: 'Queue' })).toHaveCount(0)
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

  test('the Group column is left out when every task is "ungrouped"', async ({ page }) => {
    test.skip(isRealMode, 'Mock-only test')
    mockApi?.setRegistry([
      entry('app.jobs.add', { task_group: 'ungrouped' }),
      entry('app.jobs.sub', { task_group: 'ungrouped' }),
    ])
    await loadRegistryPage(page, () => page.goto('/registry'))
    await expect(table(page).getByRole('columnheader', { name: 'Status' })).toBeVisible()
    await expect(table(page).getByRole('columnheader', { name: 'Group' })).toHaveCount(0)
  })

  test('a never-run note names two workers and counts the rest', async ({ page }) => {
    test.skip(isRealMode, 'Mock-only test')
    mockApi?.setRegistry([neverRun('app.jobs.wide', ['w-1', 'w-2', 'w-3', 'w-4', 'w-5'])])
    await loadRegistryPage(page, () => page.goto('/registry'))
    await expect(rowOf(page, 'app.jobs.wide').getByTestId('registry-note')).toHaveText(
      'Registered by w-1, w-2 and 3 more; it has not run yet.',
    )
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
      await expectFilledWithToken(block, '--anomaly')
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
