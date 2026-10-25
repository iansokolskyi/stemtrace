import { expect, type Page, test } from '@playwright/test'

import { setupMockApi } from './fixtures/mock-api'
import { createSuccessTask, type MockTask } from './fixtures/mock-data'

/**
 * The filter chips on the Executions page: every filter lives in the URL, Back restores the
 * previous ones, and the request carries exactly the filters shown. Mock mode only.
 */

test.skip(process.env.E2E_MODE === 'real', 'Mock-only test')

const PAGING = { limit: '200', offset: '0', compact: '1' }

/** A few tasks with every optional field, so the popovers have values to suggest. */
function sampleTasks(): MockTask[] {
  const base = Date.parse('2026-10-17T12:00:00Z')
  return [
    ['app.jobs.send_receipt', 'celery@w1', 'invoices', 'Billing', 'ValueError'],
    ['app.jobs.capture', 'celery@w2', 'payments', 'Billing', 'GatewayTimeout'],
    ['app.jobs.index', 'celery@w1', 'search', 'Search', null],
  ].map(([name, hostname, queue, group, exception], index) => {
    const task = createSuccessTask(name as string)
    return Object.assign(task, {
      last_updated: new Date(base - index * 1000).toISOString(),
      hostname: hostname as string,
      queue: queue as string,
      task_group: group as string,
      exception_type: exception as string | null,
      tags: group === 'Billing' ? ['money'] : [],
    })
  })
}

/** Query of every GET /api/tasks list request, in order. */
function recordTaskLists(page: Page): Record<string, string>[] {
  const seen: Record<string, string>[] = []
  page.on('request', (request) => {
    const url = new URL(request.url())
    if (url.pathname === '/api/tasks') seen.push(Object.fromEntries(url.searchParams))
  })
  return seen
}

const search = (page: Page): Record<string, string> =>
  Object.fromEntries(new URL(page.url()).searchParams)

const addFilter = (page: Page) => page.getByRole('button', { name: 'Add filter' })
const chip = (page: Page, key: string) => page.locator(`[data-chip="${key}"]`)
const popover = (page: Page) => page.getByRole('dialog')

async function open(page: Page, field: string): Promise<void> {
  await addFilter(page).click()
  await popover(page).getByRole('button', { name: field, exact: true }).click()
}

/** One way to set each filter through the UI: what it does, and the URL it must produce. */
const FILTERS: {
  key: string
  label: string
  set: (page: Page) => Promise<void>
  query: Record<string, string>
}[] = [
  {
    key: 'state',
    label: 'State',
    set: async (page) => {
      await open(page, 'State')
      await popover(page).getByRole('checkbox', { name: 'Failure' }).check()
      await popover(page).getByRole('checkbox', { name: 'Retry' }).check()
    },
    query: { state: 'FAILURE,RETRY' },
  },
  {
    key: 'name',
    label: 'Name',
    set: async (page) => {
      await open(page, 'Name')
      await popover(page).getByRole('textbox').fill('send')
      await popover(page).getByRole('textbox').press('Enter')
    },
    query: { name: 'send' },
  },
  {
    key: 'name_exact',
    label: 'Task',
    set: async (page) => {
      await open(page, 'Name')
      await popover(page).getByRole('checkbox', { name: 'Exact name' }).check()
      await popover(page).getByRole('textbox').fill('app.jobs.index')
      await popover(page).getByRole('textbox').press('Enter')
    },
    query: { name_exact: 'app.jobs.index' },
  },
  {
    key: 'exception_type',
    label: 'Exception',
    set: async (page) => {
      await open(page, 'Exception')
      await popover(page).getByRole('textbox').fill('ValueError')
      await popover(page).getByRole('textbox').press('Enter')
    },
    query: { exception_type: 'ValueError' },
  },
  {
    key: 'hostname',
    label: 'Worker',
    set: async (page) => {
      await open(page, 'Worker')
      await popover(page).getByRole('button', { name: 'celery@w2' }).click()
    },
    query: { hostname: 'celery@w2' },
  },
  {
    key: 'queue',
    label: 'Queue',
    set: async (page) => {
      await open(page, 'Queue')
      await popover(page).getByRole('textbox').fill('payments')
      await popover(page).getByRole('textbox').press('Enter')
    },
    query: { queue: 'payments' },
  },
  {
    key: 'task_group',
    label: 'Group',
    set: async (page) => {
      await open(page, 'Group')
      await popover(page).getByRole('button', { name: 'Search' }).click()
    },
    query: { task_group: 'Search' },
  },
  {
    key: 'tag',
    label: 'Tag',
    set: async (page) => {
      await open(page, 'Tag')
      await popover(page).getByRole('textbox').fill('money')
      await popover(page).getByRole('textbox').press('Enter')
    },
    query: { tag: 'money' },
  },
  {
    key: 'time',
    label: 'Time',
    set: async (page) => {
      await open(page, 'Time')
      await popover(page).getByLabel('From').fill('2026-10-01')
      await popover(page).getByLabel('To').fill('2026-10-07')
    },
    query: { from_date: '2026-10-01', to_date: '2026-10-07' },
  },
]

for (const { key, label, set, query } of FILTERS) {
  test.describe(`${label} chip`, () => {
    test('sets the URL and the request, and Back restores the previous filters', async ({
      page,
    }) => {
      const api = await setupMockApi(page, { useDefaults: false })
      api.addTasks(sampleTasks())
      const lists = recordTaskLists(page)
      await page.goto('/executions')
      await expect(page.getByText('send_receipt', { exact: true })).toBeVisible()
      await expect(chip(page, key)).toHaveCount(0)

      await set(page)

      await expect(chip(page, key)).toBeVisible()
      await expect.poll(() => search(page)).toEqual(query)
      await expect.poll(() => lists.at(-1)).toEqual({ ...PAGING, ...query })

      // Back goes to the unfiltered list: no chip, no parameter in the URL, every task listed
      // (the unfiltered answer is cached, so it may not be asked for again)
      await page.goBack()
      await expect(chip(page, key)).toHaveCount(0)
      expect(search(page)).toEqual({})
      await expect(page.locator('a[href^="/tasks/"]')).toHaveCount(3)
      // and Forward brings the filter back
      await page.goForward()
      await expect(chip(page, key)).toBeVisible()
      expect(search(page)).toEqual(query)
    })

    test('is read from the URL and removed with its × button', async ({ page }) => {
      const api = await setupMockApi(page, { useDefaults: false })
      api.addTasks(sampleTasks())
      const lists = recordTaskLists(page)
      await page.goto(`/executions?${new URLSearchParams(query)}`)
      await expect(chip(page, key)).toBeVisible()
      await expect.poll(() => lists.at(-1)).toEqual({ ...PAGING, ...query })

      await chip(page, key)
        .getByRole('button', { name: /^Remove .* filter$/ })
        .click()

      await expect(chip(page, key)).toHaveCount(0)
      expect(search(page)).toEqual({})
      await expect.poll(() => lists.at(-1)).toEqual(PAGING)
    })
  })
}

test.describe('Filters together', () => {
  test('every filter at once reaches the request, and Clear removes them all', async ({ page }) => {
    const api = await setupMockApi(page, { useDefaults: false })
    api.addTasks(sampleTasks())
    const lists = recordTaskLists(page)
    const all = {
      state: 'FAILURE,RETRY',
      name: 'app',
      name_exact: 'app.jobs.capture',
      exception_type: 'GatewayTimeout',
      hostname: 'celery@w2',
      queue: 'payments',
      task_group: 'Billing',
      tag: 'money',
      from_date: '2026-10-01',
      to_date: '2026-10-07',
    }
    await page.goto(`/executions?${new URLSearchParams(all)}`)
    for (const key of [
      'state',
      'name',
      'name_exact',
      'exception_type',
      'hostname',
      'queue',
      'task_group',
      'tag',
      'time',
    ]) {
      await expect(chip(page, key)).toBeVisible()
    }
    await expect.poll(() => lists.at(-1)).toEqual({ ...PAGING, ...all })

    await page.getByRole('button', { name: 'Clear' }).click()
    expect(search(page)).toEqual({})
    await expect.poll(() => lists.at(-1)).toEqual(PAGING)
  })

  test('the state chip is multi-select and drops itself when the last state is cleared', async ({
    page,
  }) => {
    const api = await setupMockApi(page, { useDefaults: false })
    api.addTasks(sampleTasks())
    const lists = recordTaskLists(page)
    await page.goto('/executions?state=FAILURE,RETRY')
    await expect(chip(page, 'state')).toContainText('Failure')
    await expect(chip(page, 'state')).toContainText('Retry')

    await chip(page, 'state')
      .getByRole('button', { name: /^State/ })
      .click()
    await popover(page).getByRole('checkbox', { name: 'Failure' }).uncheck()
    await expect.poll(() => search(page)).toEqual({ state: 'RETRY' })
    await expect.poll(() => lists.at(-1)).toEqual({ ...PAGING, state: 'RETRY' })

    await popover(page).getByRole('checkbox', { name: 'Success' }).check()
    await expect.poll(() => search(page)).toEqual({ state: 'RETRY,SUCCESS' })

    await popover(page).getByRole('checkbox', { name: 'Retry' }).uncheck()
    await popover(page).getByRole('checkbox', { name: 'Success' }).uncheck()
    await expect(chip(page, 'state')).toHaveCount(0)
    expect(search(page)).toEqual({})
  })

  test('invalid and empty values in the URL are dropped, not sent or shown', async ({ page }) => {
    const api = await setupMockApi(page, { useDefaults: false })
    api.addTasks(sampleTasks())
    const lists = recordTaskLists(page)
    const errors: string[] = []
    page.on('pageerror', (e) => errors.push(e.message))
    await page.goto(
      '/executions?state=bogus&name=&name_exact=%20%20&queue=&hostname=&tag=&task_group=&exception_type=&from_date=nope&to_date=2026-02-30',
    )
    await expect(page.getByText('send_receipt', { exact: true })).toBeVisible()
    await expect(page.locator('[data-chip]')).toHaveCount(0)
    expect(lists[0]).toEqual(PAGING)
    expect(errors).toEqual([])
  })

  test('states are normalised: any case, unknown ones and repeats dropped', async ({ page }) => {
    const api = await setupMockApi(page, { useDefaults: false })
    api.addTasks(sampleTasks())
    const lists = recordTaskLists(page)
    await page.goto('/executions?state=failure,nope,RETRY,failure')
    await expect(chip(page, 'state')).toBeVisible()
    await expect.poll(() => lists.at(-1)).toEqual({ ...PAGING, state: 'FAILURE,RETRY' })
  })

  test('a number in a text filter stays the text typed', async ({ page }) => {
    const api = await setupMockApi(page, { useDefaults: false })
    api.addTasks(sampleTasks())
    const lists = recordTaskLists(page)
    await page.goto('/executions?name=007&queue=42')
    await expect(chip(page, 'name')).toContainText('007')
    await expect.poll(() => lists.at(-1)).toEqual({ ...PAGING, name: '007', queue: '42' })
  })

  test('the timeline view uses the same filters and keeps them when one is added', async ({
    page,
  }) => {
    const api = await setupMockApi(page, { useDefaults: false })
    api.addTasks(sampleTasks())
    const lists = recordTaskLists(page)
    await page.goto('/executions?view=timeline&queue=payments&state=SUCCESS,FAILURE')
    await expect(chip(page, 'queue')).toBeVisible()
    await expect
      .poll(() => lists.at(-1))
      .toEqual({ limit: '50', queue: 'payments', state: 'SUCCESS,FAILURE' })

    await open(page, 'Worker')
    await popover(page).getByRole('button', { name: 'celery@w2' }).click()
    await expect
      .poll(() => search(page))
      .toEqual({
        view: 'timeline',
        queue: 'payments',
        state: 'SUCCESS,FAILURE',
        hostname: 'celery@w2',
      })
    await expect
      .poll(() => lists.at(-1))
      .toEqual({
        limit: '50',
        queue: 'payments',
        state: 'SUCCESS,FAILURE',
        hostname: 'celery@w2',
      })
  })
})

test.describe('Filter popover', () => {
  test('suggests the values in the loaded rows and in /api/groups', async ({ page }) => {
    const api = await setupMockApi(page, { useDefaults: false })
    api.addTasks(sampleTasks())
    api.setGroups([
      { name: 'Reports', source: 'code', tags: ['nightly'], color: null, task_names: [] },
    ])
    await page.goto('/executions')
    await expect(page.getByText('send_receipt', { exact: true })).toBeVisible()

    await open(page, 'Worker')
    await expect(popover(page).getByRole('button', { name: /^celery@/ })).toHaveText([
      'celery@w1',
      'celery@w2',
    ])
    await page.keyboard.press('Escape')

    await open(page, 'Group')
    // A group with no runs yet is still offered (it comes from /api/groups)
    await expect(popover(page).getByRole('button', { name: 'Reports' })).toBeVisible()
    await expect(popover(page).getByRole('button', { name: 'Billing' })).toBeVisible()
    await page.keyboard.press('Escape')

    await open(page, 'Tag')
    await expect(popover(page).getByRole('button', { name: 'nightly' })).toBeVisible()
    await expect(popover(page).getByRole('button', { name: 'money' })).toBeVisible()
  })

  test('opens and closes from the keyboard and returns focus to what opened it', async ({
    page,
  }) => {
    const api = await setupMockApi(page, { useDefaults: false })
    api.addTasks(sampleTasks())
    await page.goto('/executions?queue=payments')
    await expect(chip(page, 'queue')).toBeVisible()

    // The add button
    await addFilter(page).focus()
    await page.keyboard.press('Enter')
    await expect(popover(page)).toBeVisible()
    await page.keyboard.press('Escape')
    await expect(popover(page)).toHaveCount(0)
    await expect(addFilter(page)).toBeFocused()

    // A chip's edit button
    const edit = chip(page, 'queue').getByRole('button', { name: /^Queue/ })
    await edit.focus()
    await page.keyboard.press('Enter')
    await expect(popover(page)).toBeVisible()
    await expect(popover(page).getByRole('textbox')).toBeFocused()
    await page.keyboard.press('Escape')
    await expect(popover(page)).toHaveCount(0)
    await expect(edit).toBeFocused()

    // The remove button is a tab stop after the edit button
    await page.keyboard.press('Tab')
    await expect(
      chip(page, 'queue').getByRole('button', { name: 'Remove queue filter' }),
    ).toBeFocused()
  })

  test('editing a chip changes its value in the URL', async ({ page }) => {
    const api = await setupMockApi(page, { useDefaults: false })
    api.addTasks(sampleTasks())
    await page.goto('/executions?queue=payments')
    await chip(page, 'queue')
      .getByRole('button', { name: /^Queue/ })
      .click()
    await popover(page).getByRole('textbox').fill('search')
    await popover(page).getByRole('textbox').press('Enter')
    await expect.poll(() => search(page)).toEqual({ queue: 'search' })
    await expect(chip(page, 'queue')).toContainText('search')
  })
})

test.describe('Filter chips at 400px', () => {
  test('wrap onto more lines and the page does not scroll sideways', async ({ page }) => {
    await page.setViewportSize({ width: 400, height: 800 })
    const api = await setupMockApi(page, { useDefaults: false })
    api.addTasks(sampleTasks())
    await page.goto(
      `/executions?${new URLSearchParams({
        state: 'FAILURE,RETRY,SUCCESS',
        name_exact: 'app.jobs.a_rather_long_task_name_for_a_narrow_phone',
        exception_type: 'billing.errors.GatewayTimeoutError',
        hostname: 'celery@worker-billing-eu-west-1a',
        queue: 'payments',
        task_group: 'Billing',
        tag: 'money',
        from_date: '2026-10-01',
        to_date: '2026-10-07',
      })}`,
    )
    await expect(chip(page, 'time')).toBeVisible()
    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
    )
    expect(overflow).toBeLessThanOrEqual(0)
    // Chips sit on more than one line, and each is inside the viewport
    const tops = new Set<number>()
    for (const handle of await page.locator('[data-chip]').all()) {
      const box = await handle.boundingBox()
      tops.add(Math.round(box?.y ?? 0))
      expect((box?.x ?? 0) + (box?.width ?? 0)).toBeLessThanOrEqual(400)
    }
    expect(tops.size).toBeGreaterThan(1)
    // the popover opens inside the viewport too
    await addFilter(page).click()
    const dialog = await popover(page).boundingBox()
    expect((dialog?.x ?? 0) + (dialog?.width ?? 0)).toBeLessThanOrEqual(400)
    expect(dialog?.x ?? -1).toBeGreaterThanOrEqual(0)
  })
})
