import { expect, type Locator, type Page, test } from '@playwright/test'

import { exceptionId } from '../src/components/triage/exceptionId'
import { formatPanel, type PanelEntry, parsePanel } from '../src/components/triage/panelState'
import { encodeScope } from '../src/components/triage/runsScope'
import { flowNode } from './fixtures/flow'
import { type MockApiContext, setupMockApi } from './fixtures/mock-api'
import {
  bucketStart,
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

async function openOverview(page: Page, path = '/'): Promise<Opened> {
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
  await mockOverview(page, scenario)
  await page.goto(path)
  await expect(page.getByRole('heading', { level: 1, name: 'Overview' })).toBeVisible()
  await expect(page.getByTestId('chart-bar')).toHaveCount(12)
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
    await expect(page.getByText('All clear: no failures in the last 6h.')).toBeVisible()
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

test.describe('Overview slots', () => {
  test.skip(isRealMode, 'Mock-only test')

  test('nothing about anomalies is drawn until the anomaly work fills its slots', async ({
    page,
  }) => {
    await openOverview(page)
    await expect(
      page.getByTestId('kpi-tiles').getByRole('button', { name: /Anomalies/ }),
    ).toHaveCount(0)
    await expect(page.getByRole('heading', { name: 'Anomalies' })).toHaveCount(0)
  })
})
