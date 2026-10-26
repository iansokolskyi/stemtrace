import { expect, type Locator, type Page, test } from '@playwright/test'

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
      await popover(page).getByRole('button', { name: 'Apply' }).click()
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

/** Logs every URL the app writes to the browser history (push and replace) in `window.__urls`. */
async function logHistoryWrites(page: Page): Promise<void> {
  await page.addInitScript(() => {
    const urls: string[] = []
    ;(window as unknown as { __urls: string[] }).__urls = urls
    for (const method of ['pushState', 'replaceState'] as const) {
      const original = window.history[method].bind(window.history)
      window.history[method] = (data: unknown, unused: string, url?: string | URL | null) => {
        if (url) urls.push(String(url))
        original(data, unused, url)
      }
    }
  })
}

const writtenDates = (page: Page, key: 'from_date' | 'to_date') =>
  page.evaluate((k) => {
    const urls = (window as unknown as { __urls: string[] }).__urls
    return urls.map((u) => new URL(u, window.location.href).searchParams.get(k))
  }, key)

test.describe('Back with the popover open', () => {
  const cases = [
    {
      name: 'State',
      set: async (page: Page) => {
        await popover(page).getByRole('checkbox', { name: 'Failure' }).check()
      },
      next: async (page: Page) => {
        await popover(page).getByRole('checkbox', { name: 'Retry' }).check()
      },
    },
  ]
  for (const { name, set, next } of cases) {
    test(`${name}: Back closes the popover, and the next edit does not replace the restored entry`, async ({
      page,
    }) => {
      const api = await setupMockApi(page, { useDefaults: false })
      api.addTasks(sampleTasks())
      await page.goto('/workers')
      await page.goto('/executions')
      await open(page, name)
      await set(page)
      await expect.poll(() => Object.keys(search(page)).length).toBeGreaterThan(0)

      await page.goBack() // the popover is still open when the browser goes back
      expect(search(page)).toEqual({})
      await expect(popover(page)).toHaveCount(0)

      await open(page, name)
      await next(page)
      await expect.poll(() => Object.keys(search(page)).length).toBeGreaterThan(0)
      await page.goBack()
      // The restored entry is still there: Back reaches the unfiltered list, then the page before
      await expect(page).toHaveURL(/\/executions$/)
      await page.goBack()
      await expect(page).toHaveURL(/\/workers$/)
    })
  }
})

test.describe('Groups polling', () => {
  const groupRequests = (page: Page): { count: number } => {
    const seen = { count: 0 }
    page.on('request', (r) => {
      if (new URL(r.url()).pathname === '/api/groups') seen.count++
    })
    return seen
  }

  test('stops asking once /api/groups answers 404', async ({ page }) => {
    await page.clock.install()
    const api = await setupMockApi(page, { useDefaults: false })
    api.setGroups(null)
    const seen = groupRequests(page)
    await page.goto('/executions')
    await expect.poll(() => seen.count).toBeGreaterThanOrEqual(1)
    const first = seen.count
    await page.clock.fastForward(5 * 60 * 1000)
    // Any request the timers fired is reported before the answer to a later round trip
    await page.evaluate(() => fetch('/api/health').then((r) => r.status))
    expect(seen.count).toBe(first)
  })

  test('keeps refreshing the groups while the endpoint works', async ({ page }) => {
    await page.clock.install()
    const api = await setupMockApi(page, { useDefaults: false })
    api.setGroups([{ name: 'A', source: 'code', tags: [], color: null, task_names: [] }])
    const seen = groupRequests(page)
    await page.goto('/executions')
    await expect.poll(() => seen.count).toBeGreaterThanOrEqual(1)
    const first = seen.count
    await page.clock.fastForward(95 * 1000)
    await expect.poll(() => seen.count).toBeGreaterThan(first)
  })
})

test.describe('Popover position', () => {
  test('follows its chip when the window is resized and the chips wrap', async ({ page }) => {
    const api = await setupMockApi(page, { useDefaults: false })
    api.addTasks(sampleTasks())
    await page.goto(
      `/executions?${new URLSearchParams({
        state: 'FAILURE,RETRY,SUCCESS',
        hostname: 'celery@worker-billing-eu-west-1a',
        queue: 'payments',
        task_group: 'Billing',
      })}`,
    )
    await chip(page, 'task_group')
      .getByRole('button', { name: /^Group/ })
      .click()
    await expect(popover(page)).toBeVisible()
    await page.setViewportSize({ width: 400, height: 800 })
    // The chips wrapped; the popover is re-anchored inside the viewport
    await expect
      .poll(async () => {
        const box = await popover(page).boundingBox()
        return box ? Math.round(box.x + box.width) : 9999
      })
      .toBeLessThanOrEqual(400)
    const box = await popover(page).boundingBox()
    expect(box?.x ?? -1).toBeGreaterThanOrEqual(0)
    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
    )
    expect(overflow).toBeLessThanOrEqual(0)
  })
})

test.describe('Groups after a failed refresh', () => {
  test('one failed refresh does not stop the next ones', async ({ page }) => {
    await page.clock.install()
    const api = await setupMockApi(page, { useDefaults: false })
    api.setGroups([{ name: 'First', source: 'code', tags: [], color: null, task_names: [] }])
    await page.goto('/executions')
    const groups = page.getByRole('navigation', { name: 'Groups' })
    await expect(groups.getByRole('link', { name: 'First' })).toBeVisible()

    // The server fails once ...
    api.setGroups('error')
    await page.clock.fastForward(31 * 1000)
    await page.evaluate(() => fetch('/api/health').then((r) => r.status))
    // ... and then answers again with a group that did not exist
    api.setGroups([
      { name: 'First', source: 'code', tags: [], color: null, task_names: [] },
      { name: 'Second', source: 'code', tags: [], color: null, task_names: [] },
    ])
    await page.clock.fastForward(31 * 1000)
    await expect(groups.getByRole('link', { name: 'Second' })).toBeVisible()
  })

  test('a 404 stops the refreshes for good', async ({ page }) => {
    await page.clock.install()
    const api = await setupMockApi(page, { useDefaults: false })
    api.setGroups(null)
    let count = 0
    page.on('request', (r) => {
      if (new URL(r.url()).pathname === '/api/groups') count++
    })
    await page.goto('/executions')
    await expect.poll(() => count).toBeGreaterThanOrEqual(1)
    const first = count
    // Even if the route appears later, a server that lacked it is not asked again
    api.setGroups([{ name: 'Late', source: 'code', tags: [], color: null, task_names: [] }])
    await page.clock.fastForward(5 * 60 * 1000)
    await page.evaluate(() => fetch('/api/health').then((r) => r.status))
    expect(count).toBe(first)
  })
})

test.describe('Popover and a growing chip row', () => {
  test('a popover opened from Add filter follows the button when a chip pushes it right', async ({
    page,
  }) => {
    const api = await setupMockApi(page, { useDefaults: false })
    api.addTasks(sampleTasks())
    await page.goto('/executions')
    await addFilter(page).click()
    await popover(page).getByRole('button', { name: 'State', exact: true }).click()
    const gap = async () => {
      const dialog = await popover(page).boundingBox()
      const button = await addFilter(page).boundingBox()
      return (dialog?.x ?? 0) - (button?.x ?? 0)
    }
    expect(Math.abs(await gap())).toBeLessThanOrEqual(1)
    const before = (await addFilter(page).boundingBox())?.x ?? 0

    // A chip appears (the popover stays open for more states) and Add filter moves right
    await popover(page).getByRole('checkbox', { name: 'Failure' }).check()
    await expect(chip(page, 'state')).toBeVisible()
    await expect
      .poll(async () => (await addFilter(page).boundingBox())?.x ?? 0)
      .toBeGreaterThan(before)
    await expect.poll(async () => Math.abs(await gap())).toBeLessThanOrEqual(1)
  })
})

async function typeInto(page: Page, label: string, typed: string): Promise<void> {
  await popover(page).getByLabel(label).focus()
  await page.keyboard.type(typed)
}

const applyButton = (page: Page) => popover(page).getByRole('button', { name: 'Apply' })

test.describe('Time chip: dates apply only when asked', () => {
  // A date input reports a value on every digit of the year, so typed dates are applied only by
  // Apply or Enter, as one edit, and only when finished and valid. Anything else drops them.
  test('typing then Apply applies both dates as one history entry', async ({ page }) => {
    const api = await setupMockApi(page, { useDefaults: false })
    api.addTasks(sampleTasks())
    await logHistoryWrites(page)
    await page.goto('/workers')
    await page.goto('/executions')
    await open(page, 'Time')
    await typeInto(page, 'From', '10012025')
    await typeInto(page, 'To', '10072025')
    // Nothing has been applied while typing
    expect(search(page)).toEqual({})
    await applyButton(page).click()
    await expect
      .poll(() => search(page))
      .toEqual({ from_date: '2025-10-01', to_date: '2025-10-07' })
    await expect(popover(page)).toHaveCount(0)
    // One write for the whole edit, with both dates, and none before it
    const urls = await page.evaluate(() => (window as unknown as { __urls: string[] }).__urls)
    expect(urls.filter((u) => u.includes('from_date'))).toHaveLength(1)
    await page.goBack()
    expect(search(page)).toEqual({})
    await page.goBack()
    await expect(page).toHaveURL(/\/workers$/)
  })

  test('Enter in a date field applies', async ({ page }) => {
    const api = await setupMockApi(page, { useDefaults: false })
    api.addTasks(sampleTasks())
    await page.goto('/executions')
    await open(page, 'Time')
    await typeInto(page, 'To', '10072025')
    await page.keyboard.press('Enter')
    await expect.poll(() => search(page)).toEqual({ to_date: '2025-10-07' })
    await expect(popover(page)).toHaveCount(0)
  })

  test('no date reaches the URL by itself, however long the field sits', async ({ page }) => {
    // A fake clock: how long a finished date waits is the subject
    await page.clock.install()
    const api = await setupMockApi(page, { useDefaults: false })
    api.addTasks(sampleTasks())
    await logHistoryWrites(page)
    await page.goto('/executions')
    await open(page, 'Time')
    await typeInto(page, 'From', '10012025')
    await page.clock.fastForward(60_000)
    expect(search(page)).toEqual({})
    expect((await writtenDates(page, 'from_date')).filter((d) => d !== null)).toEqual([])
    await expect(popover(page)).toBeVisible()
  })

  test('moving between the fields applies nothing', async ({ page }) => {
    const api = await setupMockApi(page, { useDefaults: false })
    api.addTasks(sampleTasks())
    await logHistoryWrites(page)
    await page.goto('/executions')
    await open(page, 'Time')
    await typeInto(page, 'From', '10012025')
    // Focus leaves From for To, and then for the Apply button
    await popover(page).getByLabel('To').focus()
    await applyButton(page).focus()
    expect(search(page)).toEqual({})
    expect((await writtenDates(page, 'from_date')).filter((d) => d !== null)).toEqual([])
    await expect(popover(page)).toBeVisible()
  })

  test('Apply is disabled for an unfinished or rejected date, and Enter does nothing', async ({
    page,
  }) => {
    const api = await setupMockApi(page, { useDefaults: false })
    api.addTasks(sampleTasks())
    await page.goto('/executions')
    await open(page, 'Time')
    await expect(applyButton(page)).toBeEnabled() // nothing typed: nothing to reject
    const from = popover(page).getByLabel('From')
    await from.focus()
    await page.keyboard.type('10') // a month only: unfinished
    await expect(applyButton(page)).toBeDisabled()
    await page.keyboard.press('Enter')
    expect(search(page)).toEqual({})
    await page.keyboard.type('01') // the day
    await page.keyboard.type('2') // a year of 0002: a date the browser accepts and the URL rule rejects
    await expect(applyButton(page)).toBeDisabled()
    await page.keyboard.type('025')
    await expect(applyButton(page)).toBeEnabled()
    await page.keyboard.press('Enter')
    await expect.poll(() => search(page)).toEqual({ from_date: '2025-10-01' })
  })

  test('a date can be removed: empty the field, then Apply', async ({ page }) => {
    const api = await setupMockApi(page, { useDefaults: false })
    api.addTasks(sampleTasks())
    await page.goto('/executions?from_date=2026-10-01&to_date=2026-10-07')
    await chip(page, 'time').getByRole('button', { name: /^Time/ }).click()
    await popover(page).getByLabel('From').fill('')
    await applyButton(page).click()
    await expect.poll(() => search(page)).toEqual({ to_date: '2026-10-07' })
  })

  test('Apply with nothing changed closes without a history entry', async ({ page }) => {
    const api = await setupMockApi(page, { useDefaults: false })
    api.addTasks(sampleTasks())
    await logHistoryWrites(page)
    await page.goto('/executions?from_date=2026-10-01')
    await chip(page, 'time').getByRole('button', { name: /^Time/ }).click()
    await applyButton(page).click()
    await expect(popover(page)).toHaveCount(0)
    expect(await writtenDates(page, 'from_date')).toEqual([])
  })

  test('a preset applies in one click and closes the popover', async ({ page }) => {
    await page.clock.install({ time: new Date('2026-10-17T12:00:00Z') })
    const api = await setupMockApi(page, { useDefaults: false })
    api.addTasks(sampleTasks())
    await page.goto('/executions')
    await open(page, 'Time')
    await popover(page).getByRole('button', { name: 'Today' }).click()
    await expect
      .poll(() => search(page))
      .toEqual({ from_date: '2026-10-17', to_date: '2026-10-17' })
    await expect(popover(page)).toHaveCount(0)
  })
})

test.describe('Typed dates are dropped by every other way of leaving', () => {
  for (const { typed, date } of [
    { typed: '10012025', date: '2025-10-01' },
    { typed: '01011950', date: '1950-01-01' },
  ]) {
    test.describe(`${date} typed, not applied`, () => {
      test.beforeEach(async ({ page }) => {
        const api = await setupMockApi(page, { useDefaults: false })
        api.addTasks(sampleTasks())
        api.setGroups([{ name: 'Billing', source: 'code', tags: [], color: null, task_names: [] }])
        await logHistoryWrites(page)
      })

      /** The date was never applied: not in the URL, not in any history write. */
      async function expectDropped(page: Page): Promise<void> {
        expect(search(page).from_date).toBeUndefined()
        expect(search(page).to_date).toBeUndefined()
        const written = [
          ...(await writtenDates(page, 'from_date')),
          ...(await writtenDates(page, 'to_date')),
        ]
        expect(written.filter((d) => d !== null)).toEqual([])
      }

      const leaves: {
        name: string
        start: string
        leave: (page: Page) => Promise<void>
        after: (page: Page) => Promise<void>
      }[] = [
        {
          name: 'a click outside',
          start: '/executions',
          leave: (page) => page.getByRole('heading', { level: 1 }).click(),
          after: (page) => expect(popover(page)).toHaveCount(0),
        },
        {
          name: 'Escape',
          start: '/executions',
          leave: (page) => page.keyboard.press('Escape'),
          after: (page) => expect(popover(page)).toHaveCount(0),
        },
        {
          name: 'Clear',
          start: '/executions?queue=payments',
          leave: (page) => page.getByRole('button', { name: 'Clear' }).click(),
          after: async (page) => {
            await expect(popover(page)).toHaveCount(0)
            await expect.poll(() => search(page)).toEqual({})
          },
        },
        {
          name: "another chip's ×",
          start: '/executions?queue=payments',
          leave: (page) => page.getByRole('button', { name: 'Remove queue filter' }).click(),
          after: async (page) => {
            await expect(chip(page, 'queue')).toHaveCount(0)
            await expect(popover(page)).toHaveCount(0)
          },
        },
        {
          name: 'a sidebar group link',
          start: '/executions',
          leave: (page) =>
            page
              .getByRole('navigation', { name: 'Groups' })
              .getByRole('link', { name: 'Billing' })
              .click(),
          after: async (page) => {
            await expect.poll(() => search(page)).toEqual({ task_group: 'Billing' })
            await expect(popover(page)).toHaveCount(0)
          },
        },
        {
          name: 'Back',
          start: '/executions',
          leave: async (page) => {
            await page.goBack()
          },
          after: async (page) => {
            await expect(page).toHaveURL(/\/workers$/)
            await expect(popover(page)).toHaveCount(0)
          },
        },
      ]
      for (const { name, start, leave, after } of leaves) {
        test(`${name}`, async ({ page }) => {
          await page.goto('/workers')
          await page.goto(start)
          await open(page, 'Time')
          await typeInto(page, 'From', typed)
          await leave(page)
          await after(page)
          await expectDropped(page)
        })
      }

      test('Add filter, from a chip popover, opens its list and drops the date', async ({
        page,
      }) => {
        await page.goto('/executions?from_date=2026-10-01')
        await chip(page, 'time').getByRole('button', { name: /^Time/ }).click()
        await typeInto(page, 'To', typed)
        await addFilter(page).click()
        await expect(popover(page)).toHaveAccessibleName('Add filter')
        expect(search(page)).toEqual({ from_date: '2026-10-01' })
        expect(await writtenDates(page, 'to_date')).toEqual([])
      })
    })
  }
})

test.describe('Typed dates and touch', () => {
  test.use({ hasTouch: true })

  test.beforeEach(async ({ page }) => {
    const api = await setupMockApi(page, { useDefaults: false })
    api.addTasks(sampleTasks())
    await logHistoryWrites(page)
  })

  const outside = async (page: Page) => {
    const box = await page.getByRole('heading', { level: 1 }).boundingBox()
    return { x: (box?.x ?? 0) + 4, y: (box?.y ?? 0) + (box?.height ?? 0) / 2 }
  }

  test('a tap outside drops the typed date', async ({ page }) => {
    await page.goto('/executions')
    await open(page, 'Time')
    await typeInto(page, 'From', '10012025')
    const point = await outside(page)
    await page.touchscreen.tap(point.x, point.y)
    await expect(popover(page)).toHaveCount(0)
    expect(search(page)).toEqual({})
    expect((await writtenDates(page, 'from_date')).filter((d) => d !== null)).toEqual([])
  })

  test('a touch that is cancelled (a scroll taking over) applies nothing either', async ({
    page,
  }) => {
    await page.goto('/executions')
    await open(page, 'Time')
    await typeInto(page, 'From', '10012025')
    const point = await outside(page)
    const cdp = await page.context().newCDPSession(page)
    await cdp.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [point] })
    await cdp.send('Input.dispatchTouchEvent', { type: 'touchCancel', touchPoints: [] })
    await expect(popover(page)).toHaveCount(0)
    expect(search(page)).toEqual({})
    expect((await writtenDates(page, 'from_date')).filter((d) => d !== null)).toEqual([])
  })
})

test.describe('Presses on targets, with a date typed', () => {
  // Nothing may move between the press and the release: the click lands where it was aimed,
  // even when the button is held for longer than any timer. The held press uses a fake clock.
  const MODES = ['click', 'down-up', 'held'] as const

  async function press(page: Page, target: Locator, mode: (typeof MODES)[number]): Promise<void> {
    if (mode === 'click') return target.click()
    const before = await target.boundingBox()
    await page.mouse.move(
      (before?.x ?? 0) + (before?.width ?? 0) / 2,
      (before?.y ?? 0) + (before?.height ?? 0) / 2,
    )
    await page.mouse.down()
    if (mode === 'held') await page.clock.fastForward(1500)
    await page.evaluate(
      () => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done))),
    )
    // The target is where it was when the button went down
    expect(await target.boundingBox()).toEqual(before)
    await page.mouse.up()
  }

  for (const typed of ['10012025', '01011950']) {
    for (const mode of MODES) {
      test.describe(`${typed}, ${mode}`, () => {
        test.beforeEach(async ({ page }) => {
          await page.clock.install({ time: new Date('2026-10-17T12:00:00Z') })
          const api = await setupMockApi(page, { useDefaults: false })
          api.addTasks(sampleTasks())
          await logHistoryWrites(page)
        })

        const noTypedDate = async (page: Page, allowed: (string | null)[] = []) => {
          const written = await writtenDates(page, 'from_date')
          expect(written.filter((d) => d !== null && !allowed.includes(d))).toEqual([])
        }

        test('Clear clears everything and applies nothing', async ({ page }) => {
          await page.goto('/executions?state=FAILURE')
          await open(page, 'Time')
          await typeInto(page, 'From', typed)
          await press(page, page.getByRole('button', { name: 'Clear' }), mode)
          await expect.poll(() => search(page)).toEqual({})
          await noTypedDate(page)
        })

        test('a preset applies, and the typed date does not', async ({ page }) => {
          await page.goto('/executions')
          await open(page, 'Time')
          await typeInto(page, 'From', typed)
          await press(page, popover(page).getByRole('button', { name: 'Today' }), mode)
          await expect
            .poll(() => search(page))
            .toEqual({ from_date: '2026-10-17', to_date: '2026-10-17' })
          await noTypedDate(page, ['2026-10-17'])
        })

        test("another chip's × removes it, and the date is not applied", async ({ page }) => {
          await page.goto('/executions?queue=payments')
          await open(page, 'Time')
          await typeInto(page, 'From', typed)
          await press(
            page,
            chip(page, 'queue').getByRole('button', { name: 'Remove queue filter' }),
            mode,
          )
          await expect.poll(() => search(page)).toEqual({})
          await noTypedDate(page)
        })

        test('Add filter, from a chip popover, opens its list', async ({ page }) => {
          await page.goto('/executions?queue=payments&from_date=2026-10-01')
          await chip(page, 'time').getByRole('button', { name: /^Time/ }).click()
          await typeInto(page, 'To', typed)
          await press(page, addFilter(page), mode)
          await expect(popover(page)).toHaveAccessibleName('Add filter')
          expect(search(page)).toEqual({ queue: 'payments', from_date: '2026-10-01' })
          expect((await writtenDates(page, 'to_date')).filter((d) => d !== null)).toEqual([])
        })
      })
    }
  }
})
