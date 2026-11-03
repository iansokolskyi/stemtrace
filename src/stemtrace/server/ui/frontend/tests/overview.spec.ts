import { expect, type Locator, type Page, test } from '@playwright/test'

import { exceptionId } from '../src/components/triage/exceptionId'
import { formatPanel, type PanelEntry, parsePanel } from '../src/components/triage/panelState'
import { encodeScope } from '../src/components/triage/runsScope'
import { flowNode } from './fixtures/flow'
import { type MockApiContext, setupMockApi } from './fixtures/mock-api'
import { createAnomaly, createFailedTask } from './fixtures/mock-data'
import {
  advanceScenario,
  bucketStart,
  lateTask,
  mockOverview,
  type OverviewScenario,
  spikeScenario,
  spikeTasks,
} from './fixtures/overview'

/**
 * The Overview and its triage panel (PR 6). Mock mode only: the numbers come from
 * `fixtures/overview.ts`. Pointer work goes through `page.mouse` with separate down and up, and
 * text through `page.keyboard`, as a person does it.
 */

const isRealMode = process.env.E2E_MODE === 'real'
const ODD_HOST = 'celery@a/b:c%d'

interface Opened {
  api: MockApiContext
  scenario: OverviewScenario
  failedId: string
  rootId: string
}

type Setup = Opened & { page: Page }

/** `setup` shapes the scenario and the mock API before the page loads. */
async function openOverview(
  page: Page,
  path: string | ((opened: Setup) => string) = '/',
  setup?: (opened: Setup) => void | Promise<void>,
  { bars = true }: { bars?: boolean } = {},
): Promise<Opened> {
  const api = await setupMockApi(page)
  api.clearTasks()
  const scenario = spikeScenario()
  const { tasks, failedId, rootId } = spikeTasks(scenario)
  api.addTasks(tasks)
  api.workers.push({
    hostname: ODD_HOST,
    pid: 4242,
    registered_tasks: ['tests.flow.step_1'],
    status: 'online',
    registered_at: new Date(Date.now() - 3_600_000).toISOString(),
    last_seen: new Date(Date.now() - 60_000).toISOString(),
  })
  const opened = { api, scenario, failedId, rootId, page }
  await setup?.(opened)
  await mockOverview(page, scenario)
  await page.goto(typeof path === 'function' ? path(opened) : path)
  await expect(page.getByRole('heading', { level: 1, name: 'Overview' })).toBeVisible()
  if (bars) await expect(page.getByTestId('chart-bar')).toHaveCount(12)
  return { api, scenario, failedId, rootId }
}

const panelOf = (page: Page): Locator => page.getByTestId('triage-panel')
/**
 * The `panel` value the app reads: the URL's raw parameter decoded once, as the router does
 * (the router writes it encoded once more than `formatPanel` does).
 */
const panelValue = (page: Page): string | null => {
  const raw = /[?&]panel=([^&]*)/.exec(new URL(page.url()).search)?.[1]
  return raw === undefined ? null : decodeURIComponent(raw)
}

/** The page point of the middle of bucket `index` in the chart (the svg is drawn at 1:1). */
async function barPoint(page: Page, index: number): Promise<{ x: number; y: number }> {
  const box = await page.locator('[data-testid="runs-chart"] svg').boundingBox()
  if (!box) throw new Error('chart not drawn')
  const bw = (box.width - 46) / 12
  return { x: box.x + 40 + (index + 0.5) * bw, y: box.y + box.height / 2 }
}

async function clickBar(page: Page, index: number): Promise<void> {
  const { x, y } = await barPoint(page, index)
  await page.mouse.move(x, y)
  await page.mouse.down()
  await page.mouse.up()
}

test.describe('Overview', () => {
  test.skip(isRealMode, 'Mock-only test')

  test('shows the tiles, the chart, what is wrong, the workers and the health table', async ({
    page,
  }) => {
    await openOverview(page)
    const tiles = page.getByTestId('kpi-tiles')
    await expect(tiles.getByRole('button', { name: /Runs \/ min/ })).toContainText('8.4')
    await expect(tiles.getByRole('button', { name: /Failure rate/ })).toContainText('4.0%')
    await expect(tiles.getByRole('button', { name: /Failure rate/ })).toContainText('earlier: 1.0%')
    await expect(tiles.getByTestId('workers-tile')).toContainText('2/3')

    await expect(page.getByRole('table', { name: 'Failures by exception' })).toContainText(
      'ValueError',
    )
    await expect(page.getByTestId('worker-strip').getByRole('button')).toHaveCount(3)
    await expect(page.getByTestId('health-group')).toHaveCount(2)
    // Worst failure rate first, its tasks below it
    await expect(page.getByTestId('health-group').first()).toContainText('tests.flow')
  })

  test('a task with failures in the window is the loud row of the health table', async ({
    page,
  }) => {
    await openOverview(page)
    const bad = page.getByTestId('health-task').filter({ hasText: 'step_2' })
    await expect(bad).toContainText('13%')
    await expect(page.getByTestId('health-task').filter({ hasText: 'step_1' })).not.toContainText(
      'x',
    )
  })

  test('with no failures the failures block says all clear', async ({ page }) => {
    const api = await setupMockApi(page)
    api.clearTasks()
    const scenario = spikeScenario()
    scenario.failures = []
    scenario.buckets = scenario.buckets.map(() => ({ success: 10, failure: 0, retry: 0 }))
    await mockOverview(page, scenario)
    await page.goto('/')
    await expect(
      page.getByText('All clear: no failures or anomalies in the last 6h.'),
    ).toBeVisible()
  })

  test('the range asks the endpoints for its window; All sends no window to the stats', async ({
    page,
  }) => {
    const requests: string[] = []
    page.on('request', (r) => {
      if (/\/api\/(overview|failures|stats\/timeseries)/.test(r.url())) requests.push(r.url())
    })
    await openOverview(page, '/?range=1h')
    await expect.poll(() => requests.some((u) => u.includes('overview?within=1h'))).toBe(true)
    expect(requests.some((u) => u.includes('stats/timeseries?within=1h&bucket=5m'))).toBe(true)

    requests.length = 0
    await page
      .getByRole('group', { name: 'Time range' })
      .getByRole('button', { name: 'All' })
      .click()
    await expect(page).toHaveURL(/range=all/)
    await expect.poll(() => requests.some((u) => u.includes('overview?within=3650d'))).toBe(true)
    expect(requests.find((u) => u.includes('/api/failures'))).not.toContain('within=')
  })

  test('/ keeps the range and the panel, and sends old task-list filters to Executions', async ({
    page,
  }) => {
    await openOverview(page, '/?range=1h&panel=exception%3AValueError')
    await expect(page).toHaveURL(/\/\?range=1h&panel=/)
    await expect(panelOf(page)).toBeVisible()

    await page.goto('/?state=FAILURE&range=1h')
    await expect(page).toHaveURL(/\/executions\?state=FAILURE&range=1h/)
  })

  test('the Overview is in the sidebar and marked current', async ({ page }) => {
    await openOverview(page)
    await expect(
      page.getByRole('navigation', { name: 'Sections' }).getByRole('link', { name: 'Overview' }),
    ).toHaveAttribute('aria-current', 'page')
  })

  test('the chart table lists the same buckets and opens a bucket', async ({ page }) => {
    await openOverview(page)
    await page
      .getByRole('group', { name: 'Chart view' })
      .getByRole('button', { name: 'Table' })
      .click()
    await expect(
      page.getByRole('table', { name: 'Runs per bucket' }).locator('tbody tr'),
    ).toHaveCount(12)
    await page.getByTestId('runs-chart').getByRole('button').nth(0).click()
    await expect(panelOf(page)).toBeVisible()
  })
})

test.describe('Triage panel URL state', () => {
  test.skip(isRealMode, 'Mock-only test')

  const TRICKY: PanelEntry[] = [
    {
      kind: 'runs',
      id: encodeScope({
        kind: 'bar',
        from: '2026-01-01T00:00:00.000Z',
        to: '2026-01-01T00:05:00.000Z',
      }),
    },
    { kind: 'exception', id: 'a/b:c%d' },
    { kind: 'worker', id: ODD_HOST },
    { kind: 'group', id: 'g/1:x%y' },
    { kind: 'task', id: 't/1:x%y' },
    { kind: 'anomaly', id: 'k/1:x%y' },
  ]

  for (const entry of TRICKY) {
    test(`a link with a ${entry.kind} entry whose id has / : and % opens it and keeps the URL`, async ({
      page,
    }) => {
      await openOverview(page)
      const value = formatPanel([entry])
      await page.goto(`/?panel=${encodeURIComponent(value ?? '')}`)
      await expect(panelOf(page)).toBeVisible()
      await expect(panelOf(page).getByRole('heading', { level: 2 })).toBeFocused()
      expect(parsePanel(panelValue(page) ?? undefined)).toEqual([entry])
    })
  }

  test('a link with unknown kinds opens the known ones, and the next move drops the rest', async ({
    page,
  }) => {
    await openOverview(page)
    await page.goto('/?panel=bogus%3A1/exception%3AValueError/nope%3A2')
    await expect(panelOf(page).getByRole('heading', { level: 2 })).toHaveText('ValueError')
    // The task names of the group are a list; its button pushes that task's runs
    await panelOf(page).getByRole('listitem').getByRole('button', { name: 'step_2' }).click()
    expect(parsePanel(panelValue(page) ?? undefined).map((e) => e.kind)).toEqual([
      'exception',
      'runs',
    ])
  })

  test('a worker whose name has / : and % survives a reload through the app-written URL', async ({
    page,
  }) => {
    await openOverview(page)
    await page
      .getByTestId('worker-strip')
      .getByRole('button', { name: /a\/b:c%d/ })
      .click()
    expect(parsePanel(panelValue(page) ?? undefined)).toEqual([{ kind: 'worker', id: ODD_HOST }])
    await page.reload()
    await expect(panelOf(page).getByRole('heading', { level: 2 })).toHaveText('a/b:c%d')
  })

  test('every move is a history entry: Back undoes a push', async ({ page }) => {
    await openOverview(page)
    await page.getByRole('button', { name: 'ValueError' }).click()
    const first = panelValue(page)
    await panelOf(page).getByRole('button', { name: 'step_2' }).first().click()
    expect(panelValue(page)).not.toBe(first)
    await page.goBack()
    expect(panelValue(page)).toBe(first)
  })
})

test.describe('Triage panel behaviour', () => {
  test.skip(isRealMode, 'Mock-only test')

  test('Esc goes back one level, then closes, and focus returns to the opener', async ({
    page,
  }) => {
    await openOverview(page)
    const opener = page.getByRole('button', { name: 'ValueError' })
    await opener.click()
    await panelOf(page).getByRole('button', { name: 'step_2' }).first().click()
    await expect(page.locator('[aria-label="Triage path"]')).toBeVisible()

    await page.keyboard.press('Escape')
    await expect(panelOf(page).getByRole('heading', { level: 2 })).toHaveText('ValueError')
    await page.keyboard.press('Escape')
    await expect(panelOf(page)).toHaveCount(0)
    await expect(opener).toBeFocused()
    expect(panelValue(page)).toBeNull()
  })

  test('the close button closes from any depth and focus returns to the opener', async ({
    page,
  }) => {
    await openOverview(page)
    const opener = page.getByTestId('worker-strip').getByRole('button').first()
    await opener.click()
    await panelOf(page)
      .getByRole('button', { name: /Close triage panel/ })
      .click()
    await expect(panelOf(page)).toHaveCount(0)
    await expect(opener).toBeFocused()
  })

  test('closing a panel opened from a bar returns focus to the chart', async ({ page }) => {
    await openOverview(page)
    await clickBar(page, 8)
    await panelOf(page)
      .getByRole('button', { name: /Close triage panel/ })
      .click()
    await expect(page.locator('[data-testid="runs-chart"] svg')).toBeFocused()
  })

  test('the crumb shows only with two or more entries', async ({ page }) => {
    await openOverview(page)
    await page.getByRole('button', { name: 'ValueError' }).click()
    const crumb = page.getByRole('navigation', { name: 'Triage path' })
    await expect(crumb).toHaveCount(0)
    await expect(panelOf(page).getByRole('heading', { level: 2 })).toHaveText('ValueError')

    await panelOf(page).getByRole('button', { name: 'step_2' }).first().click()
    await expect(crumb).toBeVisible()
    await expect(crumb.getByRole('button', { name: 'ValueError' })).toBeVisible()
    await crumb.getByRole('button', { name: 'ValueError' }).click()
    await expect(crumb).toHaveCount(0)
  })

  test('the opener stays marked while the panel is open', async ({ page }) => {
    await openOverview(page)
    const tile = page.getByTestId('kpi-tiles').getByRole('button', { name: /Failures/ })
    await tile.click()
    await expect(tile).toHaveAttribute('data-on', '')
    await expect(tile).toHaveCSS('outline-style', 'solid')
    await page.getByRole('button', { name: /Close triage panel/ }).click()
    await expect(tile).not.toHaveAttribute('data-on', '')

    await clickBar(page, 8)
    await expect(page.getByTestId('bar-selected')).toBeVisible()
  })

  test('Esc closes the search palette first and leaves the panel open', async ({ page }) => {
    await openOverview(page)
    await page.getByRole('button', { name: 'ValueError' }).click()
    await page.keyboard.press('Control+k')
    await expect(page.getByRole('dialog')).toBeVisible()
    await page.keyboard.press('Escape')
    await expect(page.getByRole('dialog')).toHaveCount(0)
    await expect(panelOf(page)).toBeVisible()
  })

  test('the panel is an overlay at 1280px: it docks right and is not the whole screen', async ({
    page,
  }) => {
    await page.setViewportSize({ width: 1280, height: 900 })
    await openOverview(page)
    await page.getByRole('button', { name: 'ValueError' }).click()
    const box = await panelOf(page).boundingBox()
    expect(box?.width).toBeGreaterThanOrEqual(420)
    expect(box?.width).toBeLessThanOrEqual(720)
    expect((box?.x ?? 0) + (box?.width ?? 0)).toBe(1280)
  })

  test('below 900px it is a full-screen sheet with a Back button and no sideways scroll', async ({
    page,
  }) => {
    await page.setViewportSize({ width: 400, height: 800 })
    await openOverview(page)
    await page.getByRole('button', { name: 'ValueError' }).click()
    const box = await panelOf(page).boundingBox()
    expect(box).toMatchObject({ x: 0, y: 0, width: 400, height: 800 })
    await expect(panelOf(page).getByRole('button', { name: /^Back/ })).toBeVisible()
    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
    )
    expect(overflow).toBe(0)

    await panelOf(page).getByRole('button', { name: 'step_2' }).first().click()
    await panelOf(page).getByRole('button', { name: /^Back/ }).click()
    await expect(panelOf(page).getByRole('heading', { level: 2 })).toHaveText('ValueError')
    await page.keyboard.press('Escape')
    await expect(panelOf(page)).toHaveCount(0)
  })

  test('opening the panel does not move the tiles, the chart or the sections', async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 900 })
    await openOverview(page)
    const targets = [
      page.getByTestId('kpi-tiles').getByRole('button').nth(0),
      page.getByTestId('kpi-tiles').getByRole('button').nth(1),
      page.getByTestId('kpi-tiles').getByRole('button').nth(2),
      page.getByTestId('workers-tile'),
      page.getByTestId('runs-chart'),
      page.getByTestId('worker-strip'),
      page.getByRole('table', { name: 'Failures by exception' }),
    ]
    const before = await Promise.all(targets.map((t) => t.boundingBox()))
    await page.getByRole('button', { name: 'ValueError' }).click()
    await expect(panelOf(page)).toBeVisible()
    await panelOf(page)
      .getByRole('button', { name: /Expand/ })
      .click()
    const after = await Promise.all(targets.map((t) => t.boundingBox()))
    expect(after).toEqual(before)
  })

  test('a bar opens its runs, with the full-rate counts of the bucket', async ({ page }) => {
    const { scenario } = await openOverview(page)
    await clickBar(page, 8)
    const panel = panelOf(page)
    await expect(panel.getByRole('heading', { level: 2 })).toHaveText(/\d\d:\d\d–\d\d:\d\d/)
    await expect(panel).toContainText('14')
    await expect(panel).toContainText('Failure rate')
    expect(parsePanel(panelValue(page) ?? undefined)[0].id).toContain(
      encodeURIComponent(new Date(bucketStart(scenario, 8)).toISOString()),
    )
    // Only the run last updated in this bucket is listed
    await expect(panel.getByTestId('panel-run')).toHaveCount(2)
  })

  test('the runs of a bar are the ones the bucket counts, not those that merely overlap it', async ({
    page,
  }) => {
    const { scenario } = await openOverview(page)
    await clickBar(page, 1)
    await expect(panelOf(page).getByTestId('panel-run')).toHaveCount(1)
    expect(await panelOf(page).getByTestId('panel-run').first().textContent()).toContain('fetch')
    expect(bucketStart(scenario, 1)).toBeLessThan(bucketStart(scenario, 8))
  })

  test('dragging across bars brushes a span, Open runs lists it, and clear removes it', async ({
    page,
  }) => {
    await openOverview(page)
    const from = await barPoint(page, 7)
    const to = await barPoint(page, 9)
    await page.mouse.move(from.x, from.y)
    await page.mouse.down()
    await page.mouse.move((from.x + to.x) / 2, from.y)
    await page.mouse.move(to.x, to.y)
    await page.mouse.up()
    await expect(page.getByTestId('brush-label')).toHaveText(/\d\d:\d\d–\d\d:\d\d/)
    // A drag is a selection, not a click: no panel yet
    await expect(panelOf(page)).toHaveCount(0)

    await page.getByRole('button', { name: 'Open runs' }).click()
    await expect(panelOf(page).getByTestId('panel-run')).toHaveCount(2)
    await expect(page.getByTestId('brush')).toHaveAttribute('data-on', '')

    // The panel overlays the right of the chart, where the chip is: close it first
    await page.keyboard.press('Escape')
    await page.getByRole('button', { name: 'Clear time filter' }).click()
    await expect(page.getByTestId('brush-label')).toHaveCount(0)
  })

  test('the keyboard walks the buckets and Enter opens one', async ({ page }) => {
    await openOverview(page)
    await page.locator('[data-testid="runs-chart"] svg').focus()
    await page.keyboard.press('ArrowLeft')
    await page.keyboard.press('ArrowLeft')
    await page.keyboard.press('Enter')
    await expect(panelOf(page).getByRole('heading', { level: 2 })).toHaveText(/\d\d:\d\d–\d\d:\d\d/)
  })

  test('the exception row lists its failing runs through the exception filter', async ({
    page,
  }) => {
    await openOverview(page)
    const requested: string[] = []
    page.on('request', (r) => {
      if (/\/api\/tasks\?/.test(r.url())) requested.push(r.url())
    })
    await page.getByRole('button', { name: 'ValueError' }).click()
    await expect(panelOf(page).getByTestId('panel-run')).toHaveCount(1)
    expect(
      requested.some((u) => u.includes('exception_type=ValueError') && u.includes('state=FAILURE')),
    ).toBe(true)
  })

  test('failures without an exception class open with their tasks and no run list', async ({
    page,
  }) => {
    await openOverview(page)
    await page.getByRole('button', { name: 'No exception class' }).click()
    expect(panelValue(page)).toBe(`exception:${encodeURIComponent(exceptionId(null))}`)
    await expect(panelOf(page)).toContainText('carry no exception class')
    await expect(panelOf(page).getByTestId('panel-run')).toHaveCount(0)
  })

  test('a worker cell opens the worker; a host with / : and % in its name works', async ({
    page,
  }) => {
    await openOverview(page)
    await page
      .getByTestId('worker-strip')
      .getByRole('button', { name: /a\/b:c%d/ })
      .click()
    await expect(panelOf(page).getByRole('heading', { level: 2 })).toHaveText('a/b:c%d')
    await expect(panelOf(page).getByRole('link', { name: 'Open worker page' })).toHaveAttribute(
      'href',
      /\/workers\/celery%40a%2Fb%3Ac%25d$/,
    )
  })

  test('a group row opens the group and a task row opens its runs', async ({ page }) => {
    await openOverview(page)
    await page.getByTestId('health-group').first().getByRole('button').nth(1).click()
    await expect(panelOf(page).getByRole('heading', { level: 2 })).toHaveText('tests.flow')
    await expect(panelOf(page).getByRole('table', { name: 'Tasks in the group' })).toContainText(
      'step_2',
    )

    await page.keyboard.press('Escape')
    await page.getByTestId('health-task').filter({ hasText: 'step_2' }).getByRole('button').click()
    await expect(panelOf(page).getByRole('heading', { level: 2 })).toHaveText('step_2')
  })

  test('a group can be collapsed without opening the panel', async ({ page }) => {
    await openOverview(page)
    await page.getByRole('button', { name: 'Collapse tests.flow' }).click()
    await expect(page.getByTestId('health-task')).toHaveCount(1)
    await expect(panelOf(page)).toHaveCount(0)
  })
})

test.describe('Overview acceptance', () => {
  test.skip(isRealMode, 'Mock-only test')

  test('a failure spike reaches its flow with the failed node selected in three clicks, and Back restores the panel', async ({
    page,
  }) => {
    const { failedId, rootId } = await openOverview(page)
    let clicks = 0

    // 1: the failure spike's bar opens its runs
    await clickBar(page, 8)
    clicks += 1
    const run = panelOf(page).locator(`[data-testid="panel-run"][data-task-id="${failedId}"]`)
    await expect(run).toBeVisible()

    // 2: the failed run opens as the next entry
    await run.getByRole('button').click()
    clicks += 1
    await expect(panelOf(page).getByRole('link', { name: 'Open flow' })).toBeVisible()
    const panelBefore = panelValue(page)

    // 3: Open flow leaves the Overview for the flow
    await panelOf(page).getByRole('link', { name: 'Open flow' }).click()
    clicks += 1
    await expect(page).toHaveURL(new RegExp(`/flows/${rootId}$`))
    await expect(flowNode(page, failedId)).toHaveAttribute('aria-pressed', 'true')
    expect(clicks).toBeLessThanOrEqual(3)

    // Back: the Overview with the panel open at the same entry
    await page.goBack()
    await expect(page.getByRole('heading', { level: 1, name: 'Overview' })).toBeVisible()
    expect(panelValue(page)).toBe(panelBefore)
    await expect(panelOf(page)).toBeVisible()
    await expect(panelOf(page).getByRole('link', { name: 'Open flow' })).toBeVisible()
    await expect(panelOf(page).locator('h2')).toContainText(failedId.slice(0, 8))
  })
})

test.describe('Open-ended last bucket', () => {
  test.skip(isRealMode, 'Mock-only test')

  /** A failed run stamped after the last bar's end, which the server counts in that bar. */
  const late = ({ api, scenario }: Setup) => {
    scenario.buckets[11] = { success: 0, failure: 1, retry: 0 }
    api.addTask(lateTask(scenario))
  }
  const taskLists = (page: Page): string[] => {
    const urls: string[] = []
    page.on('request', (r) => {
      if (/\/api\/tasks\?/.test(r.url())) urls.push(r.url())
    })
    return urls
  }

  test('the last bar lists a run stamped after its end, and asks for no upper bound', async ({
    page,
  }) => {
    await openOverview(page, '/', late)
    const urls = taskLists(page)
    await clickBar(page, 11)
    await expect(panelOf(page).getByTestId('panel-run')).toHaveCount(1)
    expect(
      urls.filter((u) => u.includes('from_date')).every((u) => !u.includes('updated_before')),
    ).toBe(true)
  })

  test('another bar still ends at its own edge', async ({ page }) => {
    await openOverview(page, '/', late)
    const urls = taskLists(page)
    await clickBar(page, 8)
    await expect(panelOf(page).getByTestId('panel-run')).toHaveCount(2)
    expect(urls.some((u) => u.includes('updated_before'))).toBe(true)
  })

  test('a brush ending on the last bar has no upper bound either', async ({ page }) => {
    await openOverview(page, '/', late)
    const urls = taskLists(page)
    const from = await barPoint(page, 10)
    const to = await barPoint(page, 11)
    await page.mouse.move(from.x, from.y)
    await page.mouse.down()
    await page.mouse.move(to.x, to.y, { steps: 3 })
    await page.mouse.up()
    await page.getByRole('button', { name: 'Open runs' }).click()
    await expect(panelOf(page).getByTestId('panel-run')).toHaveCount(1)
    expect(
      urls.filter((u) => u.includes('from_date')).every((u) => !u.includes('updated_before')),
    ).toBe(true)
  })

  test('a brush that stops before the last bar keeps its upper bound', async ({ page }) => {
    await openOverview(page, '/', late)
    const urls = taskLists(page)
    const from = await barPoint(page, 7)
    const to = await barPoint(page, 9)
    await page.mouse.move(from.x, from.y)
    await page.mouse.down()
    await page.mouse.move(to.x, to.y, { steps: 3 })
    await page.mouse.up()
    await page.getByRole('button', { name: 'Open runs' }).click()
    await expect(panelOf(page).getByTestId('panel-run')).toHaveCount(2)
    expect(urls.some((u) => u.includes('updated_before'))).toBe(true)
  })
})

test.describe('Per-entry panel state', () => {
  test.skip(isRealMode, 'Mock-only test')

  const chip = (page: Page, name: string) =>
    panelOf(page)
      .getByRole('group', { name: 'Filter by state' })
      .getByRole('button', { name, exact: true })

  test('a state chip belongs to its entry: the next bar opens unfiltered', async ({ page }) => {
    await openOverview(page)
    await clickBar(page, 8)
    await chip(page, 'Failure').click()
    await expect(chip(page, 'Failure')).toHaveAttribute('aria-pressed', 'true')
    await expect(panelOf(page).getByTestId('panel-run')).toHaveCount(1)

    await clickBar(page, 3)
    await expect(chip(page, 'All')).toHaveAttribute('aria-pressed', 'true')
    await expect(chip(page, 'Failure')).toHaveAttribute('aria-pressed', 'false')
  })

  for (const way of ['Esc', 'browser Back'] as const) {
    test(`the chip is still on after opening a run and going back (${way})`, async ({ page }) => {
      await openOverview(page)
      await clickBar(page, 8)
      await chip(page, 'Failure').click()
      await expect(panelOf(page).getByTestId('panel-run')).toHaveCount(1)
      await panelOf(page).getByTestId('panel-run').getByRole('button').click()
      await expect(panelOf(page).getByRole('link', { name: 'Open task page' })).toBeVisible()

      if (way === 'Esc') await page.keyboard.press('Escape')
      else await page.goBack()
      await expect(chip(page, 'Failure')).toHaveAttribute('aria-pressed', 'true')
      await expect(panelOf(page).getByTestId('panel-run')).toHaveCount(1)
    })
  }

  test('Show more keeps its count when you come back from a run', async ({ page }) => {
    await openOverview(page, '/', ({ api, scenario }) => {
      for (let i = 0; i < 45; i++) {
        const task = createFailedTask('tests.flow.step_2', 'ValueError: x', 'Traceback')
        task.last_updated = new Date(bucketStart(scenario, 8) + 60_000 + i * 1000).toISOString()
        api.addTask(task)
      }
    })
    await clickBar(page, 8)
    await expect(panelOf(page).getByTestId('panel-run')).toHaveCount(40)
    await panelOf(page)
      .getByRole('button', { name: /Show \d+ more/ })
      .click()
    await expect(panelOf(page).getByTestId('panel-run')).toHaveCount(47)
    await panelOf(page).getByTestId('panel-run').first().getByRole('button').click()
    await page.keyboard.press('Escape')
    await expect(panelOf(page).getByTestId('panel-run')).toHaveCount(47)
  })
})

test.describe('Overview anomalies', () => {
  test.skip(isRealMode, 'Mock-only test')

  const withAnomalies = ({ api, failedId }: Setup) =>
    api.setAnomalies([
      createAnomaly('stuck', {
        task_id: failedId,
        name: 'tests.flow.step_2',
        hostname: 'worker-1',
        elapsed_ms: 41 * 60_000,
        threshold_ms: 22 * 60_000,
      }),
      createAnomaly('unregistered', { name: 'billing.tasks.sync_invoices', task_count: 6 }),
      createAnomaly('no_workers_online', { task_count: 4 }),
    ])

  test('the tile counts the items by kind', async ({ page }) => {
    await openOverview(page, '/', withAnomalies)
    const tile = page.getByTestId('kpi-tiles').getByRole('button', { name: /Anomalies/ })
    await expect(tile).toContainText('3')
    await expect(tile).toContainText('1 stuck, 1 unregistered, 1 no workers online')
  })

  test('the Overview reads the anomalies without asking the workers', async ({ page }) => {
    const urls: string[] = []
    page.on('request', (r) => {
      if (/\/api\/anomalies(\?|$)/.test(r.url())) urls.push(r.url())
    })
    await openOverview(page, '/', withAnomalies)
    await expect(page.getByTestId('overview-anomaly')).toHaveCount(3)
    expect(urls.length).toBeGreaterThan(0)
    expect(urls.filter((u) => !u.includes('refresh=false'))).toEqual([])
  })

  test('What is wrong lists the fleet item first, then the items, and counts them', async ({
    page,
  }) => {
    await openOverview(page, '/', withAnomalies)
    const rows = page.getByTestId('overview-anomaly')
    await expect(rows.nth(0)).toHaveAttribute('data-kind', 'no_workers_online')
    await expect(rows.nth(1)).toHaveAttribute('data-kind', 'stuck')
    await expect(rows.nth(2)).toHaveAttribute('data-kind', 'unregistered')
    await expect(page.getByText('3 anomalies now.')).toBeVisible()
  })

  test('a stuck item opens its explanation and its task, and links onward', async ({ page }) => {
    const { failedId } = await openOverview(page, '/', withAnomalies)
    await page
      .getByTestId('overview-anomaly')
      .filter({ hasText: 'step_2' })
      .getByRole('button')
      .click()
    const panel = panelOf(page)
    await expect(panel.getByRole('heading', { level: 2 })).toHaveText('Stuck step_2')
    await expect(panel.getByTestId('panel-anomaly')).toContainText(
      'Stuck: running far longer than usual',
    )
    await expect(panel.getByTestId('panel-anomaly')).toContainText('41m')
    await expect(panel.getByRole('link', { name: 'Open task page' })).toHaveAttribute(
      'href',
      new RegExp(failedId),
    )
    expect(parsePanel(panelValue(page) ?? undefined)).toEqual([
      { kind: 'anomaly', id: `stuck:${failedId}` },
    ])
  })

  test('an unregistered item opens with its name and the runs of that name', async ({ page }) => {
    await openOverview(page, '/', withAnomalies)
    await page
      .getByTestId('overview-anomaly')
      .filter({ hasText: 'sync_invoices' })
      .getByRole('button')
      .click()
    await expect(panelOf(page).getByTestId('panel-anomaly')).toContainText('6 tasks PENDING')
    await expect(panelOf(page).getByRole('heading', { name: 'Runs' })).toBeVisible()
  })

  test('the fleet item opens as an anomaly with no task below it', async ({ page }) => {
    await openOverview(page, '/', withAnomalies)
    await page.getByTestId('overview-anomaly').first().getByRole('button').click()
    await expect(panelOf(page).getByRole('heading', { level: 2 })).toHaveText('No workers online')
    await expect(panelOf(page).getByTestId('panel-anomaly')).toHaveAttribute(
      'data-kind',
      'no_workers_online',
    )
    expect(parsePanel(panelValue(page) ?? undefined)).toEqual([
      { kind: 'anomaly', id: 'no_workers_online' },
    ])
  })

  test('the tile opens the list of every item, and a row opens one on top of it', async ({
    page,
  }) => {
    await openOverview(page, '/', withAnomalies)
    await page
      .getByTestId('kpi-tiles')
      .getByRole('button', { name: /Anomalies/ })
      .click()
    await expect(panelOf(page).getByRole('heading', { level: 2 })).toHaveText('Anomalies')
    await expect(panelOf(page).getByRole('listitem')).toHaveCount(3)
    await panelOf(page)
      .getByRole('listitem')
      .filter({ hasText: 'sync_invoices' })
      .getByRole('button')
      .click()
    await expect(page.getByRole('navigation', { name: 'Triage path' })).toContainText('Anomalies')
    await page.keyboard.press('Escape')
    await expect(panelOf(page).getByRole('listitem')).toHaveCount(3)
  })

  test('a link to an item that has cleared says so', async ({ page }) => {
    await openOverview(page, '/?panel=anomaly%3Astuck%253Agone')
    await expect(panelOf(page)).toContainText('This anomaly has cleared.')
  })

  test('with failures but no anomalies the tile says None and no anomalies block is drawn', async ({
    page,
  }) => {
    await openOverview(page)
    await expect(
      page.getByTestId('kpi-tiles').getByRole('button', { name: /Anomalies/ }),
    ).toContainText('None')
    await expect(page.getByTestId('overview-anomalies')).toHaveCount(0)
    await expect(page.getByText('All clear')).toHaveCount(0)
  })

  test('with no failures and no anomalies it says all clear for both', async ({ page }) => {
    await openOverview(page, '/', ({ scenario }) => {
      scenario.failures = []
    })
    await expect(
      page.getByText('All clear: no failures or anomalies in the last 6h.'),
    ).toBeVisible()
  })

  test('with failures cleared but an anomaly present it does not say all clear', async ({
    page,
  }) => {
    await openOverview(page, '/', (setup) => {
      setup.scenario.failures = []
      withAnomalies(setup)
    })
    await expect(page.getByText('No failures in the last 6h.')).toBeVisible()
    await expect(page.getByText('All clear')).toHaveCount(0)
    await expect(page.getByTestId('overview-anomaly')).toHaveCount(3)
  })

  test('when the server has no anomalies endpoint the tile says so and nothing claims all clear', async ({
    page,
  }) => {
    await openOverview(page, '/', ({ api, scenario }) => {
      api.setAnomalies(null)
      scenario.failures = []
    })
    await expect(
      page.getByTestId('kpi-tiles').getByRole('button', { name: /Anomalies/ }),
    ).toContainText('Unavailable')
    await expect(page.getByText('All clear')).toHaveCount(0)
    await expect(page.getByText('No failures in the last 6h.')).toBeVisible()
  })

  test('the five tiles keep their place when the anomalies arrive late', async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 900 })
    await openOverview(page, '/', (setup) => {
      setup.api.setAnomalyDelay(1500)
      withAnomalies(setup)
    })
    const tiles = page.getByTestId('kpi-tiles').locator('> *')
    await expect(tiles).toHaveCount(5)
    const before = await tiles.nth(0).boundingBox()
    await expect(
      page.getByTestId('kpi-tiles').getByRole('button', { name: /Anomalies/ }),
    ).toContainText('3')
    expect(await tiles.nth(0).boundingBox()).toEqual(before)
  })
})

test.describe('Health table trend', () => {
  test.skip(isRealMode, 'Mock-only test')

  test('a group with a series draws its trend', async ({ page }) => {
    await openOverview(page)
    await expect(page.getByTestId('health-group').first().locator('svg[width="84"]')).toHaveCount(1)
  })
})

test.describe('A span stays a span when the chart moves on', () => {
  test.skip(isRealMode, 'Mock-only test')

  const late = ({ api, scenario }: Setup) => {
    scenario.buckets[11] = { success: 0, failure: 1, retry: 0 }
    api.addTask(lateTask(scenario))
  }

  for (const kind of ['bar', 'brush'] as const) {
    for (const how of ['a live refetch', 'a reload'] as const) {
      test(`a ${kind} entry on the last bucket lists with an upper bound once the next bucket starts (${how})`, async ({
        page,
      }) => {
        await page.clock.install()
        const { scenario } = await openOverview(page, '/', late)
        const urls: string[] = []
        page.on('request', (r) => {
          if (/\/api\/tasks\?/.test(r.url()) && r.url().includes('from_date')) urls.push(r.url())
        })
        if (kind === 'bar') {
          await clickBar(page, 11)
        } else {
          const from = await barPoint(page, 10)
          const to = await barPoint(page, 11)
          await page.mouse.move(from.x, from.y)
          await page.mouse.down()
          await page.mouse.move(to.x, to.y, { steps: 3 })
          await page.mouse.up()
          await page.getByRole('button', { name: 'Open runs' }).click()
        }
        // While it is the last bucket, the late run is in it
        await expect(panelOf(page).getByTestId('panel-run')).toHaveCount(1)
        expect(urls.at(-1)).not.toContain('updated_before')

        // The next bucket starts: the run stamped after the old end now belongs to the new bar
        advanceScenario(scenario)
        urls.length = 0
        if (how === 'a reload') await page.reload()
        else await page.clock.runFor(16_000)

        await expect(panelOf(page).getByTestId('panel-run')).toHaveCount(0)
        await expect(panelOf(page)).toContainText('No runs match.')
        expect(urls.at(-1)).toContain('updated_before')
        if (kind === 'brush' && how === 'a live refetch') {
          // The brush is the same span, so its button still marks it
          await expect(page.getByTestId('brush')).toHaveAttribute('data-on', '')
        }
      })
    }
  }

  test('Back across a page-level open restores the earlier entry state', async ({ page }) => {
    await openOverview(page)
    const chip = panelOf(page)
      .getByRole('group', { name: 'Filter by state' })
      .getByRole('button', { name: 'Failure', exact: true })
    await clickBar(page, 8)
    await chip.click()
    await expect(panelOf(page).getByTestId('panel-run')).toHaveCount(1)
    await clickBar(page, 3)
    await expect(chip).toHaveAttribute('aria-pressed', 'false')
    await page.goBack()
    await expect(chip).toHaveAttribute('aria-pressed', 'true')
    await expect(panelOf(page).getByTestId('panel-run')).toHaveCount(1)
  })

  test('a wrapped Anomalies note shows every kind at 1280px', async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 900 })
    await openOverview(page, '/', ({ api }) =>
      api.setAnomalies([
        createAnomaly('stuck', { task_id: 's', name: 'a.b' }),
        createAnomaly('interrupted', { task_id: 'i', name: 'a.c' }),
        createAnomaly('unregistered', { name: 'a.d' }),
        createAnomaly('no_workers_online'),
      ]),
    )
    const tile = page.getByTestId('kpi-tiles').getByRole('button', { name: /Anomalies/ })
    const text = '1 stuck, 1 interrupted, 1 unregistered, 1 no workers online'
    await expect(tile).toContainText(text)
    const box = await tile.getByText(text).boundingBox()
    const tileBox = await tile.boundingBox()
    expect((box?.y ?? 0) + (box?.height ?? 0)).toBeLessThanOrEqual(
      (tileBox?.y ?? 0) + (tileBox?.height ?? 0),
    )
  })
})

test.describe('Tile notes stay inside their band', () => {
  test.skip(isRealMode, 'Mock-only test')

  const HOSTS = ['celery@ip-10-0-12-34.eu-west-1.compute.internal', `celery@${'x'.repeat(70)}`]

  for (const width of [1280, 400]) {
    for (const hostname of HOSTS) {
      test(`a late workers answer with a long offline host moves nothing at ${width}px (${hostname.length} characters)`, async ({
        page,
      }) => {
        await page.setViewportSize({ width, height: 900 })
        await openOverview(page, '/', async ({ api, page: p }) => {
          // The workers answer arrives last, after everything else has been drawn
          await p.route(/\/api\/workers(\?|$)/, async (route) => {
            await new Promise((resolve) => setTimeout(resolve, 2500))
            await route.fallback()
          })
          api.workers.push({
            hostname,
            pid: 9,
            registered_tasks: [],
            status: 'offline',
            registered_at: new Date(Date.now() - 4 * 3_600_000).toISOString(),
            last_seen: new Date(Date.now() - 3 * 3_600_000).toISOString(),
          })
        })
        const tiles = page.getByTestId('kpi-tiles').locator('> *')
        const measure = async () => ({
          tiles: await Promise.all([0, 1, 2, 3, 4].map((i) => tiles.nth(i).boundingBox())),
          chart: (await page.getByTestId('runs-chart').boundingBox())?.y,
        })
        const before = await measure()
        const workersTile = page.getByTestId('workers-tile')
        await expect(workersTile).toContainText('offline')
        expect(await measure()).toEqual(before)
        // Nothing spills sideways: the text stays in the tile, its full text is the title
        for (let i = 0; i < 5; i++) {
          const fits = await tiles
            .nth(i)
            .evaluate((el) =>
              [...el.querySelectorAll('*')].every((c) => c.scrollWidth <= el.clientWidth),
            )
          expect(fits).toBe(true)
        }
        await expect(workersTile.locator('[title*="offline"]')).toHaveAttribute(
          'title',
          /offline: /,
        )
      })
    }
  }
})

test.describe('A span waits for the chart', () => {
  test.skip(isRealMode, 'Mock-only test')

  const late = ({ api, scenario }: Setup) => {
    scenario.buckets[11] = { success: 0, failure: 1, retry: 0 }
    api.addTask(lateTask(scenario))
  }
  /** The panel value for the last bar: the span as a client writes it (`.000Z`). */
  const lastBar = ({ scenario }: Setup): string => {
    const last = bucketStart(scenario, 11)
    const id = encodeScope({
      kind: 'bar',
      from: new Date(last).toISOString(),
      to: new Date(last + 300_000).toISOString(),
    })
    return `/?panel=${encodeURIComponent(formatPanel([{ kind: 'runs', id }]) ?? '')}`
  }
  const tasksRequests = (page: Page): string[] => {
    const urls: string[] = []
    page.on('request', (r) => {
      if (/\/api\/tasks\?/.test(r.url()) && r.url().includes('from_date')) urls.push(r.url())
    })
    return urls
  }

  test('a reload with a slow series asks once, unbounded, and never says no runs', async ({
    page,
  }) => {
    await page.addInitScript(() => {
      ;(window as unknown as { __flash: boolean }).__flash = false
      new MutationObserver(() => {
        if (document.body?.textContent?.includes('No runs match.')) {
          ;(window as unknown as { __flash: boolean }).__flash = true
        }
      }).observe(document, { subtree: true, childList: true, characterData: true })
    })
    const urls = tasksRequests(page)
    await openOverview(page, lastBar, (setup) => {
      late(setup)
      setup.scenario.seriesDelayMs = 2500
    })
    await expect(panelOf(page).getByTestId('panel-run')).toHaveCount(1)
    expect(urls).toHaveLength(1)
    expect(urls[0]).not.toContain('updated_before')
    expect(await page.evaluate(() => (window as unknown as { __flash: boolean }).__flash)).toBe(
      false,
    )
  })

  test('when the series fails the span lists with no upper bound and says the chart is unavailable', async ({
    page,
  }) => {
    const urls = tasksRequests(page)
    await openOverview(
      page,
      lastBar,
      (setup) => {
        late(setup)
        setup.scenario.seriesStatus = 500
      },
      { bars: false },
    )
    await expect(panelOf(page).getByTestId('panel-run')).toHaveCount(1)
    await expect(panelOf(page).getByTestId('chart-unavailable')).toBeVisible()
    expect(urls.every((u) => !u.includes('updated_before'))).toBe(true)
  })

  test('a link written with .000Z lines up with the server’s …Z buckets: marked, counted and open', async ({
    page,
  }) => {
    const urls = tasksRequests(page)
    await openOverview(page, lastBar, late)
    await expect(page.getByTestId('bar-selected')).toBeVisible()
    await expect(panelOf(page).getByTestId('panel-run')).toHaveCount(1)
    await expect(panelOf(page)).toContainText('Failure rate')
    expect(urls.every((u) => !u.includes('updated_before'))).toBe(true)
  })
})
