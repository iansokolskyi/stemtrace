import { expect, type Page, type Route, test } from '@playwright/test'

import { reallocationFlow, serveFlow } from './fixtures/flow-graphs'
import { flowListRoots } from './fixtures/flows-list'
import { setupMockApi } from './fixtures/mock-api'
import {
  createAnomaly,
  createFailedTask,
  createMockEvent,
  createSuccessTask,
  type MockWorker,
} from './fixtures/mock-data'
import { mockOverview, spikeScenario, spikeTasks } from './fixtures/overview'

/**
 * One error notice, one loading skeleton, one link style, and no focus box on the panel title.
 * Mock mode only: each case fails or holds back the endpoint its page reads.
 */

const isRealMode = process.env.E2E_MODE === 'real'
// The query retries three times (1 s, 2 s, 4 s) before it reports the error
const AFTER_RETRIES = { timeout: 15_000 }

async function token(page: Page, name: string): Promise<string> {
  return page.evaluate((n) => {
    const probe = document.createElement('div')
    probe.style.color = `var(--${n})`
    document.body.append(probe)
    const colour = getComputedStyle(probe).color
    probe.remove()
    return colour
  }, name)
}

const HOST = 'celery@alpha'
const worker: MockWorker = {
  hostname: HOST,
  pid: 1,
  registered_tasks: ['app.work'],
  status: 'online',
  registered_at: new Date(Date.now() - 3_600_000).toISOString(),
  last_seen: new Date(Date.now() - 60_000).toISOString(),
}

interface Case {
  name: string
  path: string
  /** The requests that fail (or never answer). */
  api: RegExp
  /** What the notice names: "Couldn't load <thing>: ...". */
  thing: string
  /** Mock setup before the page loads. */
  setup?: (page: Page) => Promise<void>
}

const withWorker = async (page: Page): Promise<void> => {
  const api = await setupMockApi(page, { useDefaults: false })
  api.setWorkers([worker])
}
const bare = async (page: Page): Promise<void> => {
  await setupMockApi(page, { useDefaults: false })
}

const CASES: Case[] = [
  { name: 'Workers', path: '/workers', api: /\/api\/workers$/, thing: 'workers', setup: bare },
  {
    name: 'Registry',
    path: '/registry',
    api: /\/api\/tasks\/registry$/,
    thing: 'the task registry',
    setup: bare,
  },
  {
    name: 'Executions',
    path: '/executions',
    api: /\/api\/tasks(\?|$)/,
    thing: 'executions',
    setup: bare,
  },
  {
    name: 'Timeline view',
    path: '/executions?view=timeline',
    api: /\/api\/tasks(\?|$)/,
    thing: 'the timeline',
    setup: bare,
  },
  { name: 'Flows', path: '/flows', api: /\/api\/graphs(\?|$)/, thing: 'flows', setup: bare },
  { name: 'Flow', path: '/flows/nope', api: /\/api\/graphs\/nope/, thing: 'the flow', setup: bare },
  { name: 'Task', path: '/tasks/nope', api: /\/api\/tasks\/nope/, thing: 'the task', setup: bare },
  {
    name: 'Anomalies',
    path: '/anomalies',
    api: /\/api\/anomalies(\?|$)/,
    thing: 'anomalies',
    setup: bare,
  },
  {
    name: 'Worker page',
    path: `/workers/${encodeURIComponent(HOST)}`,
    api: /\/api\/workers$/,
    thing: 'the worker',
    setup: bare,
  },
  {
    name: 'Worker page, recent executions',
    path: `/workers/${encodeURIComponent(HOST)}`,
    api: /\/api\/tasks(\?|$)/,
    thing: 'recent executions',
    setup: withWorker,
  },
]

test.describe('Shared error notice', () => {
  test.skip(isRealMode, 'Mock-only tests')

  for (const theme of ['light', 'dark'] as const) {
    for (const c of CASES) {
      test(`${c.name}: names what failed, has Retry, in ${theme}`, async ({ page }) => {
        await page.routeWebSocket(/\/ws$/, () => {})
        await page.addInitScript((t) => localStorage.setItem('stemtrace-theme', t), theme)
        await c.setup?.(page)
        let requests = 0
        await page.route(c.api, (route) => {
          requests += 1
          return route.fulfill({ status: 500, json: { detail: 'boom' } })
        })
        await page.goto(c.path)

        const notice = page.getByTestId('notice')
        await expect(notice).toBeVisible(AFTER_RETRIES)
        await expect(notice.getByTestId('notice-message')).toHaveText(
          `Couldn't load ${c.thing}. The server answered 500.`,
          AFTER_RETRIES,
        )
        // The client's retries are over once the button reads Retry again
        await expect(notice.getByRole('button', { name: 'Retry', exact: true })).toBeVisible(
          AFTER_RETRIES,
        )
        await expect(notice).toHaveAttribute('role', 'alert')
        await expect(notice.locator('svg').first()).toBeVisible()
        await expect(notice).toHaveCSS('background-color', await token(page, 'surface'))
        await expect(notice.getByTestId('notice-message')).toHaveCSS(
          'color',
          await token(page, 'failure-t'),
        )

        // Retry asks again
        const before = requests
        await notice.getByRole('button', { name: 'Retry', exact: true }).click()
        await expect.poll(() => requests).toBeGreaterThan(before)
      })
    }
  }

  test('the reason is one sentence from the failure: a status, no response, or none', async ({
    page,
  }) => {
    await page.routeWebSocket(/\/ws$/, () => {})
    await bare(page)
    await page.route(/\/api\/workers$/, (route) => route.fulfill({ status: 503, body: 'x' }))
    await page.goto('/workers')
    await expect(page.getByTestId('notice-message')).toHaveText(
      "Couldn't load workers. The server answered 503.",
      AFTER_RETRIES,
    )
  })

  test('Retry loads the page once the server answers', async ({ page }) => {
    await page.routeWebSocket(/\/ws$/, () => {})
    const api = await setupMockApi(page, { useDefaults: false })
    api.setWorkers([worker])
    let up = false
    await page.route(/\/api\/workers$/, (route) =>
      up ? route.fallback() : route.fulfill({ status: 500, body: 'x' }),
    )
    await page.goto('/workers')
    await expect(page.getByTestId('notice')).toBeVisible(AFTER_RETRIES)
    await expect(page.getByRole('button', { name: 'Retry', exact: true })).toBeVisible(
      AFTER_RETRIES,
    )
    up = true
    await page.getByRole('button', { name: 'Retry', exact: true }).click()
    await expect(page.getByRole('link', { name: HOST })).toBeVisible()
    await expect(page.getByTestId('notice')).toHaveCount(0)
  })

  test('the triage panel runs list shows the same notice', async ({ page }) => {
    await page.routeWebSocket(/\/ws$/, () => {})
    const api = await setupMockApi(page)
    const scenario = spikeScenario()
    api.addTasks(spikeTasks(scenario).tasks)
    await mockOverview(page, scenario)
    let requests = 0
    await page.route(/\/api\/tasks(\?|$)/, (route) => {
      requests += 1
      return route.fulfill({ status: 500, json: { detail: 'boom' } })
    })
    await page.goto('/?panel=exception%3AValueError')
    const notice = page.getByTestId('triage-panel').getByTestId('notice')
    await expect(notice.getByTestId('notice-message')).toHaveText(
      "Couldn't load runs. The server answered 500.",
      AFTER_RETRIES,
    )
    const before = requests
    await notice.getByRole('button', { name: 'Retry', exact: true }).click()
    await expect.poll(() => requests).toBeGreaterThan(before)
  })
})

const LOADING: Case[] = [
  ...CASES.filter((c) => c.name !== 'Worker page, recent executions'),
  {
    name: 'Worker page, recent executions',
    path: `/workers/${encodeURIComponent(HOST)}`,
    api: /\/api\/tasks(\?|$)/,
    thing: 'recent executions',
    setup: withWorker,
  },
]

test.describe('Shared loading skeleton', () => {
  test.skip(isRealMode, 'Mock-only tests')

  for (const c of LOADING) {
    test(`${c.name}: a skeleton shaped like the page, no text or spinner`, async ({ page }) => {
      await page.routeWebSocket(/\/ws$/, () => {})
      await c.setup?.(page)
      // The request never answers
      await page.route(c.api, () => {})
      await page.goto(c.path)

      const skeleton = page.getByTestId('skeleton').first()
      await expect(skeleton).toBeVisible()
      expect(await skeleton.locator('[data-skeleton-block]').count()).toBeGreaterThanOrEqual(3)
      await expect(skeleton.locator('[data-skeleton-block]').first()).toHaveCSS(
        'background-color',
        await token(page, 'raised'),
      )
      await expect(page.getByText(/^Loading/)).toHaveCount(0)
      await expect(page.locator('.animate-spin')).toHaveCount(0)
      await expect(page.getByTestId('notice')).toHaveCount(0)
    })
  }

  test('the Executions skeleton has a header row and several body rows', async ({ page }) => {
    await page.routeWebSocket(/\/ws$/, () => {})
    await bare(page)
    await page.route(/\/api\/tasks(\?|$)/, () => {})
    await page.goto('/executions')
    const skeleton = page.getByTestId('skeleton').first()
    await expect(skeleton).toBeVisible()
    expect(await skeleton.getByTestId('skeleton-row').count()).toBeGreaterThanOrEqual(6)
  })

  test('the panel entries hold their place with a skeleton while loading', async ({ page }) => {
    await page.routeWebSocket(/\/ws$/, () => {})
    const api = await setupMockApi(page)
    const scenario = spikeScenario()
    api.addTasks(spikeTasks(scenario).tasks)
    await mockOverview(page, scenario)
    await page.route(/\/api\/tasks(\?|$)/, () => {})
    await page.goto('/?panel=exception%3AValueError')
    await expect(page.getByTestId('triage-panel').getByTestId('skeleton')).toBeVisible()
  })
})

interface Look {
  color: string
  line: string
  decoration: string
  offset: string
}

async function lookOf(target: ReturnType<Page['locator']>): Promise<Look> {
  return target.evaluate((el) => {
    const s = getComputedStyle(el)
    return {
      color: s.color,
      line: s.textDecorationLine,
      decoration: s.textDecorationColor,
      offset: s.textUnderlineOffset,
    }
  })
}

test.describe('One link style', () => {
  test.skip(isRealMode, 'Mock-only tests')

  test('every plain link reads the same: ink text, muted underline 2px off, ink when hovered', async ({
    page,
  }) => {
    await page.routeWebSocket(/\/ws$/, () => {})
    const api = await setupMockApi(page, { useDefaults: false })
    api.setWorkers([worker])
    const task = createSuccessTask('app.work', 1)
    task.hostname = HOST
    api.addTask(task)
    const scenario = spikeScenario()
    const spike = spikeTasks(scenario)
    api.addTasks(spike.tasks)
    await mockOverview(page, scenario)

    // 1. The worker link in the task page's Details
    await page.goto(`/tasks/${task.task_id}`)
    const details = page.getByRole('link', { name: HOST })
    await expect(details).toBeVisible()
    const muted = await token(page, 'muted')
    const ink = await token(page, 'ink')
    const base = await lookOf(details)
    expect(base).toEqual({ color: ink, line: 'underline', decoration: muted, offset: '2px' })
    await details.hover()
    expect((await lookOf(details)).decoration).toBe(ink)

    const sites: [string, string, () => ReturnType<Page['locator']>][] = [
      [
        'a worker that is not there',
        '/workers/nope',
        () => page.getByRole('link', { name: 'Back to workers' }),
      ],
      [
        'a task that is not there',
        '/tasks/nope',
        () => page.getByRole('link', { name: 'Back to executions' }),
      ],
      [
        'a flow that is not there',
        '/flows/nope',
        () => page.getByRole('link', { name: 'Back to flows' }),
      ],
    ]
    for (const [label, path, link] of sites) {
      await page.goto(path)
      await expect(link(), label).toBeVisible(AFTER_RETRIES)
      expect(await lookOf(link()), label).toEqual(base)
      await link().hover()
      expect((await lookOf(link())).decoration, label).toBe(ink)
    }

    // 2. The panel's buttons that read as links
    await page.goto(`/?panel=${encodeURIComponent(`task:${spike.failedId}`)}`)
    const panelHost = page
      .getByTestId('triage-panel')
      .getByRole('button', { name: /celery|worker/i })
    if (await panelHost.count()) {
      expect(await lookOf(panelHost.first())).toEqual(base)
    }
  })

  test('breadcrumbs keep one named quiet style: muted, underlined only on hover', async ({
    page,
  }) => {
    await page.routeWebSocket(/\/ws$/, () => {})
    const api = await setupMockApi(page, { useDefaults: false })
    const task = createSuccessTask('app.work', 1)
    api.addTask(task)
    await page.goto(`/tasks/${task.task_id}`)
    const crumb = page.getByRole('navigation', { name: 'Breadcrumb' }).getByRole('link').first()
    await expect(crumb).toBeVisible()
    expect(await lookOf(crumb)).toMatchObject({
      color: await token(page, 'muted'),
      line: 'none',
    })
    await crumb.hover()
    expect(await lookOf(crumb)).toMatchObject({
      color: await token(page, 'ink'),
      line: 'underline',
      offset: '2px',
    })
  })

  test('Show traceback is the same button on the task page and in the event history', async ({
    page,
  }) => {
    await page.routeWebSocket(/\/ws$/, () => {})
    const api = await setupMockApi(page, { useDefaults: false })
    const failed = createFailedTask('app.work', 'ValueError: bad', 'Traceback (most recent call)')
    failed.events = [
      ...failed.events,
      createMockEvent(failed.task_id, failed.name, 'FAILURE', {
        exception: 'ValueError: bad',
        traceback: 'Traceback (most recent call)',
      }),
    ]
    api.addTask(failed)
    await page.goto(`/tasks/${failed.task_id}`)
    const toggles = page.getByRole('button', { name: 'Show traceback' })
    await expect.poll(() => toggles.count()).toBeGreaterThanOrEqual(2)
    const looks = await toggles.evaluateAll((els) =>
      els.map((el) => {
        const s = getComputedStyle(el)
        return [s.backgroundColor, s.color, s.height, s.textDecorationLine, s.fontWeight].join('|')
      }),
    )
    expect(new Set(looks).size).toBe(1)
    expect(await page.getByRole('link', { name: 'Show traceback' }).count()).toBe(0)
  })
})

test.describe('Panel title focus', () => {
  test.skip(isRealMode, 'Mock-only tests')

  test('the title takes focus on open without a focus box; keyboard focus elsewhere keeps its ring', async ({
    page,
  }) => {
    await page.routeWebSocket(/\/ws$/, () => {})
    const api = await setupMockApi(page)
    const scenario = spikeScenario()
    api.addTasks(spikeTasks(scenario).tasks)
    await mockOverview(page, scenario)
    await page.goto('/?panel=exception%3AValueError')
    const title = page.getByTestId('triage-panel').getByRole('heading', { level: 2 })
    await expect(title).toBeFocused()
    await expect(title).toHaveCSS('outline-style', 'none')

    // After a key press the title is still focused programmatically on the next open
    await page.keyboard.press('Tab')
    const next = page.locator(':focus')
    await expect(next).not.toHaveText((await title.textContent()) ?? '')
    await expect(next).toHaveCSS('outline-style', 'solid')
    await expect(next).toHaveCSS('outline-width', '2px')
  })
})

/** A page whose data loads, then a poll fails, then the server answers again. */
interface Recovery {
  name: string
  path: string
  api: RegExp
  setup: (page: Page) => Promise<void>
  /** Long enough for one poll and the client's retries to fail, short of the next poll. */
  waitMs: number
  /** What the page shows while it holds its data. */
  ready: (page: Page) => ReturnType<Page['locator']>
}

const RECOVERY: Recovery[] = [
  {
    name: 'Workers',
    waitMs: 38000,
    path: '/workers',
    api: /\/api\/workers$/,
    setup: withWorker,
    ready: (page) => page.getByRole('link', { name: HOST }),
  },
  {
    name: 'Worker page',
    waitMs: 38000,
    path: `/workers/${encodeURIComponent(HOST)}`,
    api: /\/api\/workers$/,
    setup: withWorker,
    ready: (page) => page.getByRole('heading', { level: 1, name: HOST }),
  },
  {
    name: 'Registry',
    waitMs: 38000,
    path: '/registry',
    api: /\/api\/tasks\/registry(\?|$)/,
    setup: async (page) => {
      await setupMockApi(page)
    },
    ready: (page) => page.getByRole('table', { name: 'Registry' }),
  },
  {
    name: 'Executions',
    waitMs: 13000,
    path: '/executions',
    api: /\/api\/tasks(\?|$)/,
    setup: async (page) => {
      await setupMockApi(page)
    },
    ready: (page) => page.getByTestId('executions-scroll'),
  },
  {
    name: 'Timeline view',
    waitMs: 13000,
    path: '/executions?view=timeline',
    api: /\/api\/tasks(\?|$)/,
    setup: async (page) => {
      await setupMockApi(page)
    },
    ready: (page) => page.getByRole('region', { name: 'Timeline' }),
  },
  {
    name: 'Flows',
    waitMs: 13000,
    path: '/flows',
    api: /\/api\/graphs(\?|$)/,
    setup: async (page) => {
      const api = await setupMockApi(page)
      api.addTasks(flowListRoots())
    },
    ready: (page) => page.getByRole('region', { name: 'Flows' }),
  },
  {
    name: 'Task',
    waitMs: 13000,
    path: '/tasks/ok-task',
    api: /\/api\/tasks\/ok-task/,
    setup: async (page) => {
      const api = await setupMockApi(page, { useDefaults: false })
      const task = createSuccessTask('app.work', 1)
      task.task_id = 'ok-task'
      api.addTask(task)
    },
    ready: (page) => page.getByRole('heading', { level: 1 }),
  },
  {
    name: 'Anomalies',
    waitMs: 31000,
    path: '/anomalies',
    api: /\/api\/anomalies(\?|$)/,
    setup: async (page) => {
      const api = await setupMockApi(page, { useDefaults: false })
      api.setAnomalies([
        createAnomaly('unregistered', { name: 'app.gone', since: new Date().toISOString() }),
      ])
    },
    ready: (page) => page.getByTestId('anomaly-item'),
  },
]

test.describe('A failed refresh keeps what the page shows', () => {
  test.skip(isRealMode, 'Mock-only tests')

  for (const c of RECOVERY) {
    test(`${c.name}: data kept, one stale line with Retry, then recovered`, async ({ page }) => {
      await page.clock.install()
      await c.setup(page)
      await page.goto(c.path)
      await expect(c.ready(page)).toBeVisible()

      const fail = (route: Route) => route.fulfill({ status: 503, body: 'x' })
      await page.route(c.api, fail)
      // The poll (5 s, or 30 s on the slower pages) and the client's three retries (1, 2, 4 s)
      await page.clock.runFor(c.waitMs)

      const line = page.getByTestId('stale-line')
      await expect(line).toBeVisible()
      await expect(line.getByRole('status')).toContainText("Couldn't refresh. Showing data from")
      await expect(c.ready(page)).toBeVisible()
      await expect(page.getByTestId('notice')).toHaveCount(0)
      await expect(page.getByText(/^Couldn't load /)).toHaveCount(0)

      await page.unroute(c.api, fail)
      await line.getByRole('button', { name: 'Retry', exact: true }).click()
      await expect(line).toHaveCount(0)
      await expect(c.ready(page)).toBeVisible()
    })
  }

  test('a failed next page keeps the loaded rows and says so at the end of the list', async ({
    page,
  }) => {
    await page.routeWebSocket(/\/ws$/, () => {})
    const api = await setupMockApi(page, { useDefaults: false })
    api.addTasks(Array.from({ length: 260 }, (_, i) => createSuccessTask(`bulk.t${i}`, i)))
    let fail = false
    await page.route(/\/api\/tasks(\?|$)/, (route) => {
      if (fail && Number(new URL(route.request().url()).searchParams.get('offset')) > 0) {
        return route.fulfill({ status: 503, body: 'x' })
      }
      return route.fallback()
    })
    await page.goto('/executions')
    await expect(page.getByTestId('executions-scroll')).toBeVisible()
    fail = true
    await page.getByTestId('executions-scroll').evaluate((el) => el.scrollTo(0, el.scrollHeight))

    const more = page.getByTestId('more-failed')
    await expect(more).toHaveText(/^Couldn't load more\. The server answered 503\./, AFTER_RETRIES)
    await expect(page.getByTestId('notice')).toHaveCount(0)
    await expect(page.getByTestId('stale-line')).toHaveCount(0)
    await expect(page.getByTestId('executions-scroll')).toBeVisible()

    fail = false
    await more.getByRole('button', { name: 'Retry', exact: true }).click()
    await expect(more).toHaveCount(0)
  })
})

test.describe('Sessions and missing items', () => {
  test.skip(isRealMode, 'Mock-only tests')

  test('a 401 with form login renders nothing where the box would be', async ({ page }) => {
    await page.routeWebSocket(/\/ws$/, () => {})
    await page.addInitScript(() => {
      // Form login is on; the redirect is held off by its cooldown so the page stays
      window.__STEMTRACE_LOGOUT_PATH__ = '/logout'
      window.sessionStorage.setItem('stemtrace:login-redirect-at', String(Date.now()))
    })
    await bare(page)
    await page.route(/\/api\/workers$/, (route) =>
      route.fulfill({ status: 401, json: { detail: 'no' } }),
    )
    await page.goto('/workers')
    await expect(page.getByRole('heading', { level: 1, name: 'Workers' })).toBeVisible()
    await page.waitForLoadState('networkidle')
    await expect(page.getByTestId('notice')).toHaveCount(0)
    await expect(page.getByText(/Couldn't load/)).toHaveCount(0)
    await expect(page.getByText('valid API key')).toHaveCount(0)
    await expect(page.getByTestId('skeleton')).toHaveCount(0)
  })

  test('a 401 without form login asks for a valid API key, with no Retry', async ({ page }) => {
    await page.routeWebSocket(/\/ws$/, () => {})
    await bare(page)
    await page.route(/\/api\/workers$/, (route) =>
      route.fulfill({ status: 401, json: { detail: 'no' } }),
    )
    await page.goto('/workers')
    await expect(page.getByTestId('notice-message')).toHaveText('The server needs a valid API key.')
    await expect(page.getByRole('button', { name: 'Retry' })).toHaveCount(0)
  })

  test('a 404 on the task page says it is no longer stored, with a way back and no Retry', async ({
    page,
  }) => {
    await page.routeWebSocket(/\/ws$/, () => {})
    await bare(page)
    let requests = 0
    await page.route(/\/api\/tasks\/gone/, (route) => {
      requests += 1
      return route.fulfill({ status: 404, json: { detail: 'not found' } })
    })
    await page.goto('/tasks/gone')
    await expect(
      page.getByText('This task is no longer stored. It may have left the retention window.'),
    ).toBeVisible()
    await expect(page.getByRole('link', { name: 'Back to executions' })).toBeVisible()
    await expect(page.getByRole('button', { name: 'Retry' })).toHaveCount(0)
    await expect(page.getByText(/404/)).toHaveCount(0)
    // A 404 is not retried (the client's first retry would come after 1 s, the second 2 s later)
    await page.waitForTimeout(3500)
    expect(requests).toBe(1)
  })

  // With data held, a second refetch would cancel the first and send another request
  // (TanStack's cancelRefetch), so the guard is what keeps it to one
  test('Retry reads "Retrying…" and ignores clicks while the request runs', async ({ page }) => {
    await page.clock.install()
    await withWorker(page)
    await page.goto('/workers')
    await expect(page.getByRole('link', { name: HOST })).toBeVisible()
    await page.route(/\/api\/workers$/, (route) => route.fulfill({ status: 503, body: 'x' }))
    await page.clock.runFor(38_000)
    const line = page.getByTestId('stale-line')
    const retry = line.getByRole('button', { name: 'Retry', exact: true })
    await expect(retry).toBeVisible()

    let requests = 0
    let release: () => void = () => {}
    const gate = new Promise<void>((resolve) => {
      release = resolve
    })
    await page.unroute(/\/api\/workers$/)
    await page.route(/\/api\/workers$/, async (route) => {
      requests += 1
      await gate
      return route.fallback()
    })
    await retry.click()
    const busy = line.getByRole('button', { name: 'Retrying…' })
    await expect(busy).toBeVisible()
    await expect(busy).toHaveAttribute('aria-disabled', 'true')
    await expect.poll(() => requests).toBe(1)
    await busy.click({ force: true })
    await busy.click({ force: true })
    await page.waitForTimeout(300)
    expect(requests).toBe(1)
    release()
    await expect(line).toHaveCount(0)
    await expect(page.getByRole('heading', { level: 1, name: 'Workers' })).toBeFocused()
  })

  test('the stale line clears once the fleet query is no longer asked for', async ({ page }) => {
    await page.clock.install()
    const api = await setupMockApi(page, { useDefaults: false })
    const since = new Date().toISOString()
    const fleet = createAnomaly('no_workers_online', { since, task_count: 14 })
    // A full list (500) cut before the fleet item, which is the newest: its own query fetches it
    const many = Array.from({ length: 501 }, (_, i) =>
      createAnomaly('unregistered', {
        name: `bulk.t${i}`,
        since: new Date(Date.now() - (600 - i) * 60_000).toISOString(),
      }),
    )
    api.setAnomalies([...many, fleet])
    await page.goto('/anomalies')
    await expect(page.getByTestId('fleet-banner')).toBeVisible({ timeout: 10_000 })

    await page.route(/\/api\/anomalies\?.*kind=no_workers_online/, (route) =>
      route.fulfill({ status: 503, body: 'x' }),
    )
    await page.clock.runFor(31_000)
    const line = page.getByTestId('stale-line')
    await expect(line).toContainText("Couldn't refresh. Showing data from")

    // The list now holds no fleet item and counts none: the fleet query is not asked for
    api.setAnomalies(many.slice(0, 3))
    await page.clock.runFor(31_000)
    await expect(line).toHaveCount(0)
    await expect(page.getByTestId('anomaly-item')).toHaveCount(3)
  })

  test('a held 503 and a 401 without form login show both lines', async ({ page }) => {
    await page.clock.install()
    const api = await setupMockApi(page, { useDefaults: false })
    api.setWorkers([worker])
    await page.goto(`/workers/${encodeURIComponent(HOST)}`)
    await expect(page.getByRole('heading', { level: 1, name: HOST })).toBeVisible()
    await page.route(/\/api\/workers$/, (route) => route.fulfill({ status: 503, body: 'x' }))
    await page.route(/\/api\/tasks(\?|$)/, (route) =>
      route.fulfill({ status: 401, json: { detail: 'no' } }),
    )
    await page.clock.runFor(45_000)
    const line = page.getByTestId('stale-line')
    await expect(line.getByText("Couldn't refresh. Showing data from")).toBeVisible()
    await expect(line.getByText('The server needs a valid API key.')).toBeVisible()
  })

  test('the palette says Searching while a source is still on its first load, not No results', async ({
    page,
  }) => {
    await page.routeWebSocket(/\/ws$/, () => {})
    await bare(page)
    // The workers list never answers
    await page.route(/\/api\/workers(\?|$)/, () => {})
    await page.goto('/flows')
    // The shortcut is bound once the app has rendered
    await expect(page.getByRole('heading', { level: 1, name: 'Flows' })).toBeVisible()
    await page.keyboard.press('Control+k')
    const dialog = page.getByRole('dialog', { name: 'Search' })
    await dialog.getByRole('combobox').fill('zzz-no-such-thing')
    await expect(dialog.getByRole('status')).toHaveText('Searching…')
    await expect(dialog.getByText(/No results/)).toHaveCount(0)
  })
})

// Held empty data, placeholder rows, next pages, evicted items and the inspector (readings O19-O24)

const fail503 = (route: Route) => route.fulfill({ status: 503, body: 'x' })

test.describe('An empty list that loaded is held data', () => {
  test.skip(isRealMode, 'Mock-only tests')

  interface EmptyCase {
    name: string
    path: string
    api: RegExp
    waitMs: number
    setup: (page: Page) => Promise<void>
    empty: (page: Page) => ReturnType<Page['locator']>
  }
  const CASES: EmptyCase[] = [
    {
      name: 'Executions',
      path: '/executions',
      api: /\/api\/tasks(\?|$)/,
      waitMs: 13_000,
      setup: bare,
      empty: (page) => page.getByTestId('executions-empty'),
    },
    {
      name: 'Timeline view',
      path: '/executions?view=timeline',
      api: /\/api\/tasks(\?|$)/,
      waitMs: 13_000,
      setup: bare,
      empty: (page) => page.getByText('No executions to display'),
    },
    {
      name: 'Worker page without that worker',
      path: `/workers/${encodeURIComponent(HOST)}`,
      api: /\/api\/workers$/,
      waitMs: 38_000,
      setup: async (page) => {
        const api = await setupMockApi(page, { useDefaults: false })
        api.setWorkers([{ ...worker, hostname: 'celery@other' }])
      },
      empty: (page) => page.getByText(`No worker named ${HOST}`),
    },
    {
      name: 'Triage panel runs',
      path: '/?panel=exception%3AValueError',
      api: /\/api\/tasks(\?|$)/,
      waitMs: 24_000,
      setup: async (page) => {
        const api = await setupMockApi(page)
        api.clearTasks()
        await mockOverview(page, spikeScenario())
      },
      empty: (page) => page.getByTestId('triage-panel').getByText('No runs match.'),
    },
  ]

  for (const c of CASES) {
    test(`${c.name}: the empty state stays under one stale line, then recovers`, async ({
      page,
    }) => {
      await page.clock.install()
      await c.setup(page)
      await page.goto(c.path)
      await expect(c.empty(page)).toBeVisible()

      await page.route(c.api, fail503)
      await page.clock.runFor(c.waitMs)

      const line = page.getByTestId('stale-line')
      await expect(line.getByRole('status')).toContainText("Couldn't refresh. Showing data from")
      await expect(c.empty(page)).toBeVisible()
      await expect(page.getByTestId('notice')).toHaveCount(0)

      await page.unroute(c.api, fail503)
      await line.getByRole('button', { name: 'Retry', exact: true }).click()
      await expect(line).toHaveCount(0)
      await expect(c.empty(page)).toBeVisible()
    })
  }
})

test.describe('Show more in the triage panel', () => {
  test.skip(isRealMode, 'Mock-only tests')

  test('a failed longer list keeps the rows and says it could not load more', async ({ page }) => {
    await page.routeWebSocket(/\/ws$/, () => {})
    const api = await setupMockApi(page)
    api.clearTasks()
    for (let i = 0; i < 45; i++) {
      api.addTask(createFailedTask('tests.flow.step_2', 'ValueError: x', 'Traceback'))
    }
    await mockOverview(page, spikeScenario())
    await page.goto('/?panel=exception%3AValueError')
    const panel = page.getByTestId('triage-panel')
    const more = panel.getByRole('button', { name: /Show \d+ more/ })
    await expect(more).toBeVisible()
    const rows = panel.getByTestId('panel-run')
    const before = await rows.count()

    const failLonger = (route: Route) =>
      Number(new URL(route.request().url()).searchParams.get('limit')) > before
        ? route.fulfill({ status: 503, body: 'x' })
        : route.fallback()
    await page.route(/\/api\/tasks(\?|$)/, failLonger)
    await more.click()

    const failed = panel.getByTestId('more-failed')
    await expect(failed).toHaveText(
      /^Couldn't load more\. The server answered 503\./,
      AFTER_RETRIES,
    )
    await expect(rows).toHaveCount(before)
    // The rows are the shorter list's, so no time of its own to show
    await expect(panel.getByTestId('stale-line')).toHaveCount(0)
    await expect(panel.getByTestId('notice')).toHaveCount(0)

    await page.unroute(/\/api\/tasks(\?|$)/, failLonger)
    await failed.getByRole('button', { name: 'Retry', exact: true }).click()
    await expect(failed).toHaveCount(0)
    await expect(rows).toHaveCount(Math.min(45, before + 10))
  })
})

type ListMode = 'ok' | 'next fails' | 'next unauthorized' | 'all fail'

interface PagedList {
  name: string
  path: string
  /** Opens the list with two pages and scrolls the sentinel into view once it is loaded. */
  open: (page: Page, mode: () => ListMode, hits: number[]) => Promise<void>
  scrollToEnd: (page: Page) => Promise<void>
  ready: (page: Page) => ReturnType<Page['locator']>
}

const flowPages = (page: Page, mode: () => ListMode, hits: number[]) => {
  const roots = Array.from({ length: 60 }, (_, index) => {
    const task = createSuccessTask(`app.jobs.job_${String(index).padStart(2, '0')}`)
    return Object.assign(task, { task_id: `flow-page-${index}`, root_id: `flow-page-${index}` })
  })
  return page.route(/\/api\/graphs(\?|$)/, (route) => {
    const offset = Number(new URL(route.request().url()).searchParams.get('offset') ?? 0)
    const now = mode()
    if (now === 'all fail' || (offset > 0 && now === 'next fails')) {
      if (offset > 0) hits.push(offset)
      return route.fulfill({ status: 503, body: 'x' })
    }
    if (offset > 0 && now === 'next unauthorized') {
      hits.push(offset)
      return route.fulfill({ status: 401, json: { detail: 'no' } })
    }
    if (offset > 0) hits.push(offset)
    return route.fulfill({
      json: {
        graphs: roots.slice(offset, offset + 50).map((root) => ({ ...root, children: [] })),
        total: 60,
        limit: 50,
        offset,
      },
    })
  })
}

const executionPages = async (page: Page, mode: () => ListMode, hits: number[]) => {
  const api = await setupMockApi(page, { useDefaults: false })
  api.addTasks(Array.from({ length: 260 }, (_, i) => createSuccessTask(`bulk.t${i}`, i)))
  await page.route(/\/api\/tasks(\?|$)/, (route) => {
    const offset = Number(new URL(route.request().url()).searchParams.get('offset') ?? 0)
    const now = mode()
    if (offset > 0) hits.push(offset)
    if (now === 'all fail' || (offset > 0 && now === 'next fails')) {
      return route.fulfill({ status: 503, body: 'x' })
    }
    if (offset > 0 && now === 'next unauthorized') {
      return route.fulfill({ status: 401, json: { detail: 'no' } })
    }
    return route.fallback()
  })
}

const PAGED: PagedList[] = [
  {
    name: 'Executions',
    path: '/executions',
    open: async (page, mode, hits) => {
      await executionPages(page, mode, hits)
    },
    scrollToEnd: (page) =>
      page.getByTestId('executions-scroll').evaluate((el) => el.scrollTo(0, el.scrollHeight)),
    ready: (page) => page.getByTestId('executions-scroll'),
  },
  {
    name: 'Flows',
    path: '/flows',
    open: async (page, mode, hits) => {
      await setupMockApi(page, { useDefaults: false })
      await flowPages(page, mode, hits)
    },
    scrollToEnd: async (page) => {
      await page
        .getByRole('region', { name: 'Flows' })
        .locator('> *')
        .last()
        .scrollIntoViewIfNeeded()
    },
    ready: (page) => page.getByRole('region', { name: 'Flows' }),
  },
]

test.describe('Next pages of Executions and Flows', () => {
  test.skip(isRealMode, 'Mock-only tests')

  for (const list of PAGED) {
    test(`${list.name}: a 401 on the next page without form login asks for an API key`, async ({
      page,
    }) => {
      await page.routeWebSocket(/\/ws$/, () => {})
      let mode: ListMode = 'ok'
      await list.open(page, () => mode, [])
      await page.goto(list.path)
      await expect(list.ready(page)).toBeVisible()
      mode = 'next unauthorized'
      await list.scrollToEnd(page)
      const failed = page.getByTestId('more-failed')
      await expect(failed).toHaveText('The server needs a valid API key.')
      await expect(page.getByText(/Couldn't load more/)).toHaveCount(0)
      await expect(failed.getByRole('button')).toHaveCount(0)
    })

    test(`${list.name}: a 401 on the next page with form login shows nothing`, async ({ page }) => {
      await page.routeWebSocket(/\/ws$/, () => {})
      await page.addInitScript(() => {
        window.__STEMTRACE_LOGOUT_PATH__ = '/logout'
        window.sessionStorage.setItem('stemtrace:login-redirect-at', String(Date.now()))
      })
      let mode: ListMode = 'ok'
      const hits: number[] = []
      await list.open(page, () => mode, hits)
      await page.goto(list.path)
      await expect(list.ready(page)).toBeVisible()
      mode = 'next unauthorized'
      await list.scrollToEnd(page)
      await expect.poll(() => hits.length).toBeGreaterThan(0)
      await page.waitForLoadState('networkidle')
      await expect(page.getByTestId('more-failed')).toHaveCount(0)
      await expect(page.getByText(/Couldn't load more/)).toHaveCount(0)
    })

    test(`${list.name}: polls never re-ask a failed next page`, async ({ page }) => {
      await page.clock.install()
      let mode: ListMode = 'ok'
      const hits: number[] = []
      await list.open(page, () => mode, hits)
      await page.goto(list.path)
      await expect(list.ready(page)).toBeVisible()
      mode = 'next fails'
      await list.scrollToEnd(page)
      // The client's three retries (1, 2, 4 s), then the box
      await page.clock.runFor(8_000)
      await expect(page.getByTestId('more-failed')).toBeVisible()
      const asked = hits.length
      expect(asked).toBeGreaterThan(0)

      // Six polls of the first page succeed; none asks for the next page again
      await page.clock.runFor(32_000)
      expect(hits.length).toBe(asked)
      await expect(page.getByTestId('more-failed')).toBeVisible()
      await expect(page.getByTestId('stale-line')).toHaveCount(0)
    })

    test(`${list.name}: the server going down after a next-page failure shows both lines`, async ({
      page,
    }) => {
      await page.clock.install()
      let mode: ListMode = 'ok'
      await list.open(page, () => mode, [])
      await page.goto(list.path)
      await expect(list.ready(page)).toBeVisible()
      mode = 'next fails'
      await list.scrollToEnd(page)
      await page.clock.runFor(8_000)
      await expect(page.getByTestId('more-failed')).toBeVisible()

      mode = 'all fail'
      await page.clock.runFor(13_000)
      await expect(page.getByTestId('stale-line').getByRole('status')).toContainText(
        "Couldn't refresh. Showing data from",
      )
      await expect(page.getByTestId('more-failed')).toBeVisible()
      await expect(list.ready(page)).toBeVisible()
    })
  }
})

test.describe('An item that is no longer stored, with data held', () => {
  test.skip(isRealMode, 'Mock-only tests')

  const goneLine = (what: string) =>
    new RegExp(`^This ${what} is no longer stored\\. Showing data from \\d\\d:\\d\\d UTC\\.$`)

  for (const c of [
    {
      name: 'Task page',
      what: 'task',
      path: '/tasks/ok-task',
      api: /\/api\/tasks\/ok-task/,
      setup: async (page: Page) => {
        const api = await setupMockApi(page, { useDefaults: false })
        const task = createSuccessTask('app.work', 1)
        task.task_id = 'ok-task'
        api.addTask(task)
      },
      ready: (page: Page) => page.getByRole('heading', { level: 1 }),
    },
    {
      name: 'Flow page',
      what: 'flow',
      path: '/flows/root',
      api: /\/api\/graphs\/root/,
      setup: async (page: Page) => {
        await setupMockApi(page, { useDefaults: false })
        await serveFlow(page, reallocationFlow())
      },
      ready: (page: Page) => page.getByRole('region', { name: 'Timeline' }),
    },
    {
      name: 'Triage panel task',
      what: 'task',
      path: '',
      api: /\/api\/tasks\/[^/?]+(\?.*)?$/,
      setup: async (page: Page) => {
        const api = await setupMockApi(page)
        const scenario = spikeScenario()
        const spike = spikeTasks(scenario)
        api.addTasks(spike.tasks)
        await mockOverview(page, scenario)
        c_panelPath = `/?panel=${encodeURIComponent(`task:${spike.failedId}`)}`
      },
      ready: (page: Page) =>
        page.getByTestId('triage-panel').getByRole('link', { name: 'Open task page' }),
    },
  ]) {
    test(`${c.name}: the stale line says so, has no Retry, and polling stops`, async ({ page }) => {
      await page.clock.install()
      await c.setup(page)
      await page.goto(c.path || c_panelPath)
      await expect(c.ready(page)).toBeVisible()

      let hits = 0
      await page.route(c.api, (route) => {
        hits += 1
        return route.fulfill({ status: 404, json: { detail: 'not found' } })
      })
      await page.clock.runFor(13_000)
      const line = page.getByTestId('stale-line')
      await expect(line.getByRole('status')).toHaveText(goneLine(c.what))
      await expect(line.getByRole('button')).toHaveCount(0)
      await expect(c.ready(page)).toBeVisible()
      await expect(page.getByText(/Couldn't refresh/)).toHaveCount(0)

      // No retry of a 404, and no poll after it
      const seen = hits
      expect(seen).toBeGreaterThan(0)
      await page.clock.runFor(40_000)
      expect(hits).toBe(seen)
    })
  }
})

let c_panelPath = ''

test.describe('The flow inspector joins the page', () => {
  test.skip(isRealMode, 'Mock-only tests')

  test('a failed refresh of the chosen task shows in the page stale line', async ({ page }) => {
    await page.clock.install()
    await setupMockApi(page, { useDefaults: false })
    await serveFlow(page, reallocationFlow())
    await page.goto('/flows/root')
    const inspector = page.getByRole('complementary', { name: 'Inspector' })
    await expect(inspector.getByRole('link', { name: 'Open full detail' })).toBeVisible()

    await page.route(/\/api\/tasks\/[^/?]+(\?.*)?$/, fail503)
    await page.clock.runFor(13_000)
    await expect(page.getByTestId('stale-line').getByRole('status')).toContainText(
      "Couldn't refresh. Showing data from",
    )
    await expect(page.getByTestId('notice')).toHaveCount(0)
    await expect(inspector.getByRole('link', { name: 'Open full detail' })).toBeVisible()
  })

  test('with no task data the inspector says so in place of the missing fields', async ({
    page,
  }) => {
    await page.routeWebSocket(/\/ws$/, () => {})
    await setupMockApi(page, { useDefaults: false })
    await serveFlow(page, reallocationFlow())
    await page.route(/\/api\/tasks\/[^/?]+(\?.*)?$/, fail503)
    await page.goto('/flows/root')
    const inspector = page.getByRole('complementary', { name: 'Inspector' })
    await expect(inspector.getByText("Couldn't load the task details.")).toBeVisible(AFTER_RETRIES)
    await expect(inspector.getByRole('link', { name: 'Open full detail' })).toBeVisible()
    await expect(page.getByTestId('notice')).toHaveCount(0)
  })
})
