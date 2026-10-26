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

test.describe('Time chip typing', () => {
  // Chromium's date input emits a value on every digit of the year: typing 2025 passes through
  // 0002, 0020, 0202 and 2025. Only a finished date may reach the URL or the input.
  for (const key of ['from_date', 'to_date'] as const) {
    const label = key === 'from_date' ? 'From' : 'To'
    const other = key === 'from_date' ? 'to_date=2026-10-20' : 'from_date=2026-09-01'
    for (const start of ['a set date', 'an empty field'] as const) {
      test(`typing a year into ${label} from ${start} writes only the finished date`, async ({
        page,
      }) => {
        const api = await setupMockApi(page, { useDefaults: false })
        api.addTasks(sampleTasks())
        await logHistoryWrites(page)
        const initial = start === 'a set date' ? `&${key}=2026-10-01` : ''
        await page.goto(`/executions?${other}${initial}`)
        await chip(page, 'time').getByRole('button', { name: /^Time/ }).click()
        const input = popover(page).getByLabel(label)
        await input.focus()
        // month, day, then the four digits of the year, as a person types them
        await page.keyboard.type('10012025')
        // The finished date settles into the URL by itself
        await expect.poll(() => search(page)[key]).toBe('2025-10-01')
        expect(await input.inputValue()).toBe('2025-10-01')
        // Nothing but the finished date, and the date that was there, ever reached the URL
        const written = await writtenDates(page, key)
        expect(written.filter((d) => d !== '2025-10-01' && d !== '2026-10-01')).toEqual([])
        // The chip never lost its dates while typing, and the popover is still open
        await expect(chip(page, 'time')).toBeVisible()
        await expect(popover(page)).toBeVisible()
      })
    }
  }

  test('an unfinished date is not written, and leaving the field puts the old value back', async ({
    page,
  }) => {
    // A fake clock: the time a date has to sit unfinished before it could count is the subject
    await page.clock.install()
    const api = await setupMockApi(page, { useDefaults: false })
    api.addTasks(sampleTasks())
    await logHistoryWrites(page)
    await page.goto('/executions?from_date=2026-10-01&to_date=2026-10-20')
    await chip(page, 'time').getByRole('button', { name: /^Time/ }).click()
    const from = popover(page).getByLabel('From')
    await from.focus()
    await page.keyboard.press('Backspace') // clears the month segment: an incomplete date
    await page.clock.fastForward(5000)
    expect(await writtenDates(page, 'from_date')).toEqual([])
    expect(search(page)).toEqual({ from_date: '2026-10-01', to_date: '2026-10-20' })

    // Leave the field with the entry unfinished
    await popover(page).getByLabel('To').focus()
    await expect(from).toHaveValue('2026-10-01')
    expect(search(page)).toEqual({ from_date: '2026-10-01', to_date: '2026-10-20' })
  })

  test('Escape while editing cancels the edit first, then closes the popover', async ({ page }) => {
    const api = await setupMockApi(page, { useDefaults: false })
    api.addTasks(sampleTasks())
    await page.goto('/executions?from_date=2026-10-01')
    await chip(page, 'time').getByRole('button', { name: /^Time/ }).click()
    const from = popover(page).getByLabel('From')
    await from.focus()
    await page.keyboard.press('Backspace')
    await page.keyboard.press('Escape')
    // The edit is cancelled and the popover stays; the date is as it was
    await expect(popover(page)).toBeVisible()
    await expect(from).toHaveValue('2026-10-01')
    expect(search(page)).toEqual({ from_date: '2026-10-01' })
    await page.keyboard.press('Escape')
    await expect(popover(page)).toHaveCount(0)
  })

  test('Enter commits a typed date at once, and one finished edit is one history entry', async ({
    page,
  }) => {
    const api = await setupMockApi(page, { useDefaults: false })
    api.addTasks(sampleTasks())
    await page.goto('/workers')
    await page.goto('/executions')
    await addFilter(page).click()
    await popover(page).getByRole('button', { name: 'Time', exact: true }).click()
    await popover(page).getByLabel('From').focus()
    await page.keyboard.type('10012025')
    await page.keyboard.press('Enter')
    await expect.poll(() => search(page)).toEqual({ from_date: '2025-10-01' })
    await popover(page).getByLabel('To').focus()
    await page.keyboard.type('10072025')
    await page.keyboard.press('Enter')
    await expect
      .poll(() => search(page))
      .toEqual({ from_date: '2025-10-01', to_date: '2025-10-07' })
    // Both dates were one edit: one Back reaches the unfiltered list, a second leaves the page
    await page.goBack()
    expect(search(page)).toEqual({})
    await page.goBack()
    await expect(page).toHaveURL(/\/workers$/)
  })
})

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
    {
      name: 'Time',
      set: async (page: Page) => {
        await popover(page).getByLabel('From').fill('2026-10-01')
      },
      next: async (page: Page) => {
        await popover(page).getByLabel('To').fill('2026-10-07')
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

test.describe('Typed date and an outside click', () => {
  // A click outside closes the popover. The date typed so far must not be lost with it, whether
  // or not the 400 ms it takes to settle on its own has passed (a year before 1970 never does).
  for (const { typed, date } of [
    { typed: '10012025', date: '2025-10-01' },
    { typed: '01011950', date: '1950-01-01' },
  ]) {
    test(`${date} typed and then a click outside is kept`, async ({ page }) => {
      const api = await setupMockApi(page, { useDefaults: false })
      api.addTasks(sampleTasks())
      await page.goto('/executions')
      await addFilter(page).click()
      await popover(page).getByRole('button', { name: 'Time', exact: true }).click()
      await popover(page).getByLabel('From').focus()
      await page.keyboard.type(typed)
      await page.getByRole('heading', { level: 1 }).click()
      await expect(popover(page)).toHaveCount(0)
      await expect.poll(() => search(page)).toEqual({ from_date: date })
      await expect(chip(page, 'time')).toBeVisible()
    })
  }

  test('an unfinished date is dropped by a click outside, not written', async ({ page }) => {
    const api = await setupMockApi(page, { useDefaults: false })
    api.addTasks(sampleTasks())
    await page.goto('/executions?from_date=2026-10-01')
    await chip(page, 'time').getByRole('button', { name: /^Time/ }).click()
    await popover(page).getByLabel('From').focus()
    await page.keyboard.press('Backspace')
    await page.getByRole('heading', { level: 1 }).click()
    await expect(popover(page)).toHaveCount(0)
    expect(search(page)).toEqual({ from_date: '2026-10-01' })
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
      return Math.round((dialog?.x ?? 0) - (button?.x ?? 0))
    }
    expect(await gap()).toBe(0)
    const before = (await addFilter(page).boundingBox())?.x ?? 0

    // A chip appears (the popover stays open for more states) and Add filter moves right
    await popover(page).getByRole('checkbox', { name: 'Failure' }).check()
    await expect(chip(page, 'state')).toBeVisible()
    await expect
      .poll(async () => (await addFilter(page).boundingBox())?.x ?? 0)
      .toBeGreaterThan(before)
    await expect.poll(gap).toBe(0)
  })
})
