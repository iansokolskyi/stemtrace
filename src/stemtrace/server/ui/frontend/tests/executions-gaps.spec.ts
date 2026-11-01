import { expect, type Locator, type Page, test } from '@playwright/test'

import { setupMockApi } from './fixtures/mock-api'
import {
  createFailedTask,
  createRegistryEntry,
  createSuccessTask,
  type MockRegisteredTask,
} from './fixtures/mock-data'

/**
 * The Executions page details of the prototype: the empty result and its "Clear filters", the
 * "registered but never run" sentence, the one-row toolbar, the "Filter by" menu and state
 * order, the breadcrumb for one task name, and leaving a date view. Mock mode only.
 */

test.skip(process.env.E2E_MODE === 'real', 'Mock-only test')

const filterButton = (page: Page): Locator =>
  page.getByRole('button', { name: 'Filter', exact: true })
const popover = (page: Page): Locator => page.getByRole('dialog')
const empty = (page: Page): Locator => page.getByTestId('executions-empty')
const crumbs = (page: Page): Locator => page.getByRole('navigation', { name: 'Breadcrumb' })

/** The query of the current URL as [key, value] pairs, in URL order. */
const search = (page: Page): [string, string][] => [...new URL(page.url()).searchParams]

/** Opens Filter, Time range and picks a preset day, which replaces the range with dates. */
async function pickDay(page: Page, preset: string): Promise<void> {
  await filterButton(page).click()
  await popover(page).getByRole('button', { name: 'Time range', exact: true }).click()
  await popover(page).getByRole('button', { name: preset }).click()
}

const neverRun = (name: string): MockRegisteredTask => ({
  ...createRegistryEntry(name, 0),
  last_run: null,
  status: 'never_run',
})

test.describe('Empty result', () => {
  test('keeps the header and says what is empty, for the range', async ({ page }) => {
    await setupMockApi(page, { useDefaults: false })
    const cases: [string, string][] = [
      ['/executions?queue=nowhere', 'No executions match these filters in the last 6h.'],
      ['/executions?queue=nowhere&range=1h', 'No executions match these filters in the last hour.'],
      ['/executions?queue=nowhere&range=all', 'No executions match these filters.'],
      [
        '/executions?queue=nowhere&from_date=2026-10-01&to_date=2026-10-02',
        'No executions match these filters in this time window.',
      ],
    ]
    for (const [url, text] of cases) {
      await page.goto(url)
      await expect(page.getByTestId('executions-empty-text')).toHaveText(text)
      await expect(page.locator('[data-column-header]')).toHaveText(['State', 'Task', 'Updated'])
    }
  })

  test('Clear filters removes the filters and keeps the range', async ({ page }) => {
    await setupMockApi(page, { useDefaults: false })
    await page.goto('/executions?queue=nowhere&state=FAILURE&range=1h')
    await empty(page).getByRole('button', { name: 'Clear filters' }).click()
    await expect.poll(() => search(page)).toEqual([['range', '1h']])
    await expect(page.locator('[data-chip]')).toHaveCount(0)
    await expect(page.getByRole('button', { name: '1h', exact: true })).toHaveAttribute(
      'aria-pressed',
      'true',
    )
  })

  test('Clear filters works from the keyboard', async ({ page }) => {
    await setupMockApi(page, { useDefaults: false })
    await page.goto('/executions?queue=nowhere')
    const clear = empty(page).getByRole('button', { name: 'Clear filters' })
    await clear.focus()
    await page.keyboard.press('Enter')
    await expect.poll(() => search(page)).toEqual([])
  })

  test('with no filter says the page is empty, with a hint and no button', async ({ page }) => {
    await setupMockApi(page, { useDefaults: false })
    const cases: [string, string][] = [
      ['/executions', 'No executions in the last 6h.'],
      ['/executions?range=1h', 'No executions in the last hour.'],
      ['/executions?range=all', 'No executions.'],
    ]
    for (const [url, text] of cases) {
      await page.goto(url)
      await expect(page.getByTestId('executions-empty-text')).toHaveText(text)
      await expect(empty(page).getByText('Run a Celery task to see it here.')).toBeVisible()
      await expect(empty(page).getByRole('button')).toHaveCount(0)
    }
  })

  test('with filters there is no hint', async ({ page }) => {
    await setupMockApi(page, { useDefaults: false })
    await page.goto('/executions?queue=nowhere')
    await expect(empty(page).getByText('Run a Celery task')).toHaveCount(0)
  })

  test('at 400px the text and button sit inside the box, the page does not scroll', async ({
    page,
  }) => {
    await page.setViewportSize({ width: 400, height: 800 })
    const api = await setupMockApi(page, { useDefaults: false })
    api.setRegistry([neverRun('app.jobs.quiet')])
    for (const url of [
      '/executions?queue=nowhere&state=FAILURE',
      '/executions?name_exact=app.jobs.quiet',
      '/executions',
    ]) {
      await page.goto(url)
      await expect(empty(page)).toBeVisible()
      if (url.includes('quiet')) {
        await expect(page.getByTestId('executions-empty-text')).toContainText('has never run.')
      }
      const box = await page.getByTestId('executions-box').boundingBox()
      for (const part of [
        page.getByTestId('executions-empty-text'),
        empty(page).getByRole('button'),
      ]) {
        if ((await part.count()) === 0) continue
        const rect = await part.boundingBox()
        expect(rect?.x ?? -1).toBeGreaterThanOrEqual(box?.x ?? 0)
        expect((rect?.x ?? 0) + (rect?.width ?? 0)).toBeLessThanOrEqual(
          (box?.x ?? 0) + (box?.width ?? 0) + 0.5,
        )
      }
      expect(
        await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth),
      ).toBe(true)
    }
  })
})

test.describe('A task that is registered but has never run', () => {
  const sentence = 'This task is registered but has never run.'

  test('says so when the registry lists the name as never_run', async ({ page }) => {
    const api = await setupMockApi(page, { useDefaults: false })
    api.setRegistry([createRegistryEntry('app.other'), neverRun('app.jobs.quiet')])
    const requests: URL[] = []
    page.on('request', (request) => {
      const url = new URL(request.url())
      if (url.pathname === '/api/tasks/registry') requests.push(url)
    })
    await page.goto('/executions?name_exact=app.jobs.quiet')
    await expect(page.getByTestId('executions-empty-text')).toHaveText(
      `No executions match these filters in the last 6h. ${sentence}`,
    )
    // Without asking the workers (issue #122): the sidebar's request is the only one
    expect(requests.map((url) => [...url.searchParams])).toEqual([[['refresh', 'false']]])
  })

  test('says nothing when the name is registered and has run', async ({ page }) => {
    const api = await setupMockApi(page, { useDefaults: false })
    api.setRegistry([createRegistryEntry('app.jobs.quiet', 3)])
    await page.goto('/executions?name_exact=app.jobs.quiet')
    await expect(page.getByTestId('executions-empty-text')).toHaveText(
      'No executions match these filters in the last 6h.',
    )
  })

  test('says nothing when the registry does not list the name', async ({ page }) => {
    const api = await setupMockApi(page, { useDefaults: false })
    api.setRegistry([neverRun('app.jobs.other')])
    await page.goto('/executions?name_exact=app.jobs.quiet')
    await expect(page.getByTestId('executions-empty-text')).toHaveText(
      'No executions match these filters in the last 6h.',
    )
  })

  test('says nothing when the registry request fails', async ({ page }) => {
    await setupMockApi(page, { useDefaults: false })
    await page.route(/\/api\/tasks\/registry/, (route) =>
      route.fulfill({ status: 500, contentType: 'application/json', body: '{}' }),
    )
    await page.goto('/executions?name_exact=app.jobs.quiet')
    await expect(page.getByTestId('executions-empty-text')).toHaveText(
      'No executions match these filters in the last 6h.',
    )
  })

  test('never asks the workers, with or without rows', async ({ page }) => {
    const api = await setupMockApi(page, { useDefaults: false })
    api.setRegistry([neverRun('app.jobs.quiet')])
    api.addTask(createSuccessTask('app.jobs.busy'))
    const requests: string[] = []
    page.on('request', (request) => {
      if (new URL(request.url()).pathname === '/api/tasks/registry') requests.push(request.url())
    })
    await page.goto('/executions?name=quiet')
    await expect(empty(page)).toBeVisible()
    await page.goto('/executions?name_exact=app.jobs.busy')
    await expect(page.locator('a[href^="/tasks/"]')).toHaveCount(1)
    // Every registry request leaves the workers alone (issue #122)
    expect(requests.map((url) => new URL(url).searchParams.get('refresh'))).toEqual([
      'false',
      'false',
    ])
  })
})

test.describe('Toolbar', () => {
  test('is one row: chips, Filter and Clear all on the left, density and view on the right', async ({
    page,
  }) => {
    const api = await setupMockApi(page, { useDefaults: false })
    api.addTask(createFailedTask('app.jobs.bad', 'ValueError: boom', 'tb'))
    await page.goto('/executions?state=FAILURE')
    const chip = page.locator('[data-chip="state"]')
    const parts = [
      chip,
      filterButton(page),
      page.getByRole('button', { name: 'Clear all', exact: true }),
      page.getByRole('group', { name: 'Row density' }),
      page.getByRole('group', { name: 'View' }),
    ]
    const centres: number[] = []
    for (const part of parts) {
      const box = await part.boundingBox()
      centres.push((box?.y ?? -99) + (box?.height ?? 0) / 2)
    }
    for (const centre of centres) expect(Math.abs(centre - centres[0])).toBeLessThan(3)
    const xs: number[] = []
    for (const part of parts) xs.push((await part.boundingBox())?.x ?? -1)
    expect(xs).toEqual([...xs].sort((a, b) => a - b))
    const heading = await page.getByRole('heading', { level: 1 }).boundingBox()
    expect(centres[0]).toBeGreaterThan((heading?.y ?? 0) + (heading?.height ?? 0))
  })

  test('the chip reads "State Failure" with a remove button, and Clear all clears', async ({
    page,
  }) => {
    await setupMockApi(page, { useDefaults: false })
    await page.goto('/executions?state=FAILURE')
    const chip = page.locator('[data-chip="state"]')
    await expect(chip).toHaveText(/^State\s*Failure\s*×$/)
    await expect(chip).not.toContainText(':')
    await expect(page.getByRole('button', { name: 'Add filter' })).toHaveCount(0)
    await page.getByRole('button', { name: 'Clear all', exact: true }).click()
    await expect.poll(() => search(page)).toEqual([])
    await expect(page.getByRole('button', { name: 'Clear all' })).toHaveCount(0)
  })

  test('at 400px the toolbar wraps and the page does not scroll sideways', async ({ page }) => {
    await page.setViewportSize({ width: 400, height: 800 })
    await setupMockApi(page, { useDefaults: false })
    await page.goto('/executions?state=FAILURE&queue=payments&hostname=celery@worker-1')
    await expect(filterButton(page)).toBeVisible()
    await expect(page.getByRole('group', { name: 'View' })).toBeInViewport({ ratio: 1 })
    expect(
      await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth),
    ).toBe(true)
  })
})

test.describe('Filter menu', () => {
  test('has a "Filter by" header and the prototype order, then the extra fields', async ({
    page,
  }) => {
    await setupMockApi(page, { useDefaults: false })
    await page.goto('/executions')
    await filterButton(page).click()
    await expect(popover(page)).toHaveAccessibleName('Filter by')
    await expect(popover(page).getByText('Filter by', { exact: true })).toBeVisible()
    await expect(popover(page).getByRole('button')).toHaveText([
      'State',
      'Group',
      'Task name',
      'Worker',
      'Queue',
      'Time range',
      'Exception',
      'Tag',
    ])
  })

  test('the state editor lists failure, retry, running, pending, success first', async ({
    page,
  }) => {
    await setupMockApi(page, { useDefaults: false })
    await page.goto('/executions')
    await filterButton(page).click()
    await popover(page).getByRole('button', { name: 'State', exact: true }).click()
    await expect(popover(page).getByRole('checkbox')).toHaveCount(9)
    await expect(popover(page).locator('label')).toHaveText([
      'Failure',
      'Retry',
      'Running',
      'Pending',
      'Success',
      'Received',
      'Revoked',
      'Rejected',
      'Ignored',
    ])
  })

  test('the URL keeps raw states, in the order they were picked', async ({ page }) => {
    await setupMockApi(page, { useDefaults: false })
    await page.goto('/executions')
    await filterButton(page).click()
    await popover(page).getByRole('button', { name: 'State', exact: true }).click()
    await popover(page).getByRole('checkbox', { name: 'Running' }).check()
    await popover(page).getByRole('checkbox', { name: 'Failure' }).check()
    await expect.poll(() => search(page)).toEqual([['state', 'STARTED,FAILURE']])
    await expect(page.locator('[data-chip="state"]')).toHaveText(/^State\s*Running, Failure\s*×$/)
  })

  test('works from the keyboard: open, pick State, toggle Failure, Escape returns focus', async ({
    page,
  }) => {
    await setupMockApi(page, { useDefaults: false })
    await page.goto('/executions')
    await filterButton(page).focus()
    await page.keyboard.press('Enter')
    await expect(popover(page).getByRole('button', { name: 'State', exact: true })).toBeFocused()
    await page.keyboard.press('Enter')
    await expect(popover(page).getByRole('checkbox', { name: 'Failure' })).toBeFocused()
    await page.keyboard.press('Space')
    await expect.poll(() => search(page)).toEqual([['state', 'FAILURE']])
    await page.keyboard.press('Escape')
    await expect(popover(page)).toHaveCount(0)
    await expect(filterButton(page)).toBeFocused()
  })
})

test.describe('Breadcrumb', () => {
  test('for one task name reads "Executions > name", the name in mono', async ({ page }) => {
    await setupMockApi(page, { useDefaults: false })
    await page.goto('/executions?name_exact=app.jobs.quiet')
    await expect(crumbs(page).getByRole('link')).toHaveText(['Executions'])
    const last = crumbs(page).locator('span[aria-current="page"]')
    await expect(last).toHaveText('app.jobs.quiet')
    await expect(last).toHaveClass(/font-mono/)
    await expect(last).toHaveAttribute('title', 'app.jobs.quiet')
  })

  test('the Executions link clears the name and keeps the range', async ({ page }) => {
    await setupMockApi(page, { useDefaults: false })
    await page.goto('/executions?name_exact=app.jobs.quiet&range=1h')
    await crumbs(page).getByRole('link', { name: 'Executions' }).click()
    await expect.poll(() => search(page)).toEqual([['range', '1h']])
    await expect(crumbs(page).getByRole('link')).toHaveCount(0)
    await expect(crumbs(page)).toHaveText('Executions')
  })

  test('with only a substring name filter it is just "Executions"', async ({ page }) => {
    await setupMockApi(page, { useDefaults: false })
    await page.goto('/executions?name=quiet')
    await expect(crumbs(page)).toHaveText('Executions')
  })
})

test.describe('Leaving a date view', () => {
  const dates = '2026-10-01'

  test('removing the date chip returns to the range the page showed before', async ({ page }) => {
    await setupMockApi(page, { useDefaults: false })
    await page.goto('/executions?range=1h')
    await pickDay(page, 'Today')
    await expect(page.locator('[data-chip="time"]')).toBeVisible()
    expect(search(page).map(([key]) => key)).toEqual(['from_date', 'to_date'])

    await page.getByRole('button', { name: 'Remove time filter' }).click()
    await expect.poll(() => search(page)).toEqual([['range', '1h']])
    await expect(page.getByRole('button', { name: '1h', exact: true })).toHaveAttribute(
      'aria-pressed',
      'true',
    )
  })

  test('Clear all returns to the same range', async ({ page }) => {
    await setupMockApi(page, { useDefaults: false })
    await page.goto('/executions?range=all')
    await pickDay(page, 'Yesterday')
    await page.getByRole('button', { name: 'Clear all', exact: true }).click()
    await expect.poll(() => search(page)).toEqual([['range', 'all']])
  })

  test('with no range carried, removing the chip returns to the default', async ({ page }) => {
    await setupMockApi(page, { useDefaults: false })
    await page.goto(`/executions?from_date=${dates}`)
    await page.getByRole('button', { name: 'Remove time filter' }).click()
    await expect.poll(() => search(page)).toEqual([])
    await expect(page.getByRole('button', { name: '6h', exact: true })).toHaveAttribute(
      'aria-pressed',
      'true',
    )
  })

  test('picking a range in the strip lands on the same window', async ({ page }) => {
    await setupMockApi(page, { useDefaults: false })
    await page.goto('/executions?range=1h')
    await pickDay(page, 'Today')
    await page.getByRole('button', { name: '1h', exact: true }).click()
    await expect.poll(() => search(page)).toEqual([['range', '1h']])
  })
})
