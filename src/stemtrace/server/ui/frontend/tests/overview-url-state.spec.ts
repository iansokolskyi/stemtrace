import { expect, type Locator, type Page, test } from '@playwright/test'

import { formatBrush, parseBrush } from '../src/components/overview/brushSpan'
import { formatPanel, type PanelEntry, parsePanel } from '../src/components/triage/panelState'
import { encodeScope } from '../src/components/triage/runsScope'
import { type MockApiContext, setupMockApi } from './fixtures/mock-api'
import { createFailedTask } from './fixtures/mock-data'
import {
  bucketStart,
  mockOverview,
  type OverviewScenario,
  spikeScenario,
  spikeTasks,
} from './fixtures/overview'

/**
 * The Overview's view state in the URL: the chips and "Show more" of a panel entry (`@s=`, `@m=`
 * after the entry in `panel`) and the chart's brush (`brush=<from>-<to>`, epoch seconds).
 */

const isRealMode = process.env.E2E_MODE === 'real'

test.describe('panel entry view: parse and format', () => {
  test('writes the chips and the Show more count after the entry', () => {
    const entry: PanelEntry = { kind: 'runs', id: 'a', states: ['FAILURE', 'RETRY'], more: 2 }
    expect(formatPanel([entry])).toBe('runs:a@s=FAILURE,RETRY@m=2')
    expect(parsePanel('runs:a@s=FAILURE,RETRY@m=2')).toStrictEqual([entry])
  })

  test('defaults are not written: no chips, no extra pages', () => {
    expect(formatPanel([{ kind: 'runs', id: 'a', states: [], more: 0 }])).toBe('runs:a')
    expect(formatPanel([{ kind: 'runs', id: 'a', states: ['SUCCESS'] }])).toBe('runs:a@s=SUCCESS')
    expect(formatPanel([{ kind: 'runs', id: 'a', more: 3 }])).toBe('runs:a@m=3')
  })

  test('each entry of a stack keeps its own view', () => {
    const stack: PanelEntry[] = [
      { kind: 'runs', id: 'x', states: ['FAILURE'], more: 1 },
      { kind: 'task', id: 't1' },
      { kind: 'exception', id: 'E', more: 4 },
    ]
    expect(parsePanel(formatPanel(stack))).toStrictEqual(stack)
  })

  for (const id of ['a@b', 'a@s=FAILURE', '@m=9', 'a/b:c%d@']) {
    test(`an id with an at sign round-trips with a view: ${JSON.stringify(id)}`, () => {
      const entry: PanelEntry = { kind: 'runs', id, states: ['RETRY'], more: 1 }
      expect(parsePanel(formatPanel([entry]))).toStrictEqual([entry])
    })
  }

  test('an unknown or repeated chip is dropped, the order of the rest is kept', () => {
    expect(parsePanel('runs:a@s=bogus,retry,FAILURE,RETRY')).toStrictEqual([
      { kind: 'runs', id: 'a', states: ['RETRY', 'FAILURE'] },
    ])
    expect(parsePanel('runs:a@s=bogus')).toStrictEqual([{ kind: 'runs', id: 'a' }])
    expect(parsePanel('runs:a@s=')).toStrictEqual([{ kind: 'runs', id: 'a' }])
  })

  for (const bad of ['x', '', '0', '-1', '1.5', '14', '100', '1e1', '99999999999999999999', ' 2']) {
    test(`a Show more count of ${JSON.stringify(bad)} is the default`, () => {
      expect(parsePanel(`runs:a@m=${bad}`)).toStrictEqual([{ kind: 'runs', id: 'a' }])
    })
  }

  test('an at sign that does not start a view key stays in the id', () => {
    expect(parsePanel('worker:celery@host1')).toStrictEqual([
      { kind: 'worker', id: 'celery@host1' },
    ])
    expect(parsePanel('worker:celery@host1@m=2/runs:a@s2')).toStrictEqual([
      { kind: 'worker', id: 'celery@host1', more: 2 },
      { kind: 'runs', id: 'a@s2' },
    ])
  })

  test('the largest Show more count is 13', () => {
    expect(parsePanel('runs:a@m=13')).toStrictEqual([{ kind: 'runs', id: 'a', more: 13 }])
  })

  test('the first of a repeated view key wins', () => {
    expect(parsePanel('runs:a@m=2@m=3')).toStrictEqual([{ kind: 'runs', id: 'a', more: 2 }])
  })
})

test.describe('brush: parse and format', () => {
  const FROM = '2026-11-26T10:05:00Z'
  const TO = '2026-11-26T10:20:00Z'
  const seconds = (iso: string) => Date.parse(iso) / 1000

  test('writes the span as epoch seconds and reads it back as the same instants', () => {
    const value = formatBrush({ from: FROM, to: TO })
    expect(value).toBe(`${seconds(FROM)}-${seconds(TO)}`)
    expect(parseBrush(value)).toStrictEqual({ from: FROM, to: TO })
  })

  test('another spelling of the same instants writes the same value', () => {
    expect(formatBrush({ from: '2026-11-26T10:05:00.000Z', to: '2026-11-26T10:20:00+00:00' })).toBe(
      formatBrush({ from: FROM, to: TO }),
    )
  })

  test('no brush has no value', () => {
    expect(formatBrush(null)).toBeUndefined()
    expect(parseBrush(undefined)).toBeNull()
  })

  for (const bad of [
    'abc',
    '',
    '-',
    '1764151500',
    '1764151500-',
    '-1764151500',
    '1764152400-1764151500',
    '1764151500-1764151500',
    '1764151500-abc',
    '1.5-2.5',
    '17641515000000000-17641515000000001',
    '1764151500--1764152400',
  ]) {
    test(`a brush value of ${JSON.stringify(bad)} is no brush`, () => {
      expect(parseBrush(bad)).toBeNull()
    })
  }
})

type Setup = (api: MockApiContext, scenario: OverviewScenario) => void

async function openOverview(
  page: Page,
  path = '/',
  setup?: Setup,
): Promise<{ scenario: OverviewScenario }> {
  const api = await setupMockApi(page)
  api.clearTasks()
  const scenario = spikeScenario()
  api.addTasks(spikeTasks(scenario).tasks)
  setup?.(api, scenario)
  await mockOverview(page, scenario)
  await page.goto(path)
  await expect(page.getByRole('heading', { level: 1, name: 'Overview' })).toBeVisible()
  await expect(page.getByTestId('chart-bar')).toHaveCount(12)
  return { scenario }
}

const panelOf = (page: Page): Locator => page.getByTestId('triage-panel')
const chip = (page: Page, name: string): Locator =>
  panelOf(page)
    .getByRole('group', { name: 'Filter by state' })
    .getByRole('button', { name, exact: true })
const search = (page: Page): URLSearchParams => new URL(page.url()).searchParams
const historyLength = (page: Page): Promise<number> => page.evaluate(() => history.length)

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

async function brushBars(page: Page, a: number, b: number): Promise<void> {
  const from = await barPoint(page, a)
  const to = await barPoint(page, b)
  await page.mouse.move(from.x, from.y)
  await page.mouse.down()
  await page.mouse.move((from.x + to.x) / 2, from.y)
  await page.mouse.move(to.x, to.y)
  await page.mouse.up()
}

const addFailures: Setup = (api, scenario) => {
  for (let i = 0; i < 45; i++) {
    const task = createFailedTask('tests.flow.step_2', 'ValueError: x', 'Traceback')
    task.last_updated = new Date(bucketStart(scenario, 8) + 60_000 + i * 1000).toISOString()
    api.addTask(task)
  }
}

const showMore = (page: Page): Locator =>
  panelOf(page).getByRole('button', { name: /Show \d+ more/ })

test.describe('Overview view state in the URL', () => {
  test.skip(isRealMode, 'Mock-only test')

  test('a chip survives a reload and is written after the entry', async ({ page }) => {
    await openOverview(page)
    await clickBar(page, 8)
    await chip(page, 'Failure').click()
    await expect(panelOf(page).getByTestId('panel-run')).toHaveCount(1)
    expect(search(page).get('panel')).toMatch(/@s=FAILURE$/)

    await page.reload()
    await expect(chip(page, 'Failure')).toHaveAttribute('aria-pressed', 'true')
    await expect(chip(page, 'All')).toHaveAttribute('aria-pressed', 'false')
    await expect(panelOf(page).getByTestId('panel-run')).toHaveCount(1)
  })

  test('two chips keep the order they were picked in', async ({ page }) => {
    await openOverview(page)
    await clickBar(page, 8)
    await chip(page, 'Retry').click()
    await chip(page, 'Failure').click()
    expect(search(page).get('panel')).toMatch(/@s=RETRY,FAILURE$/)
    await chip(page, 'All').click()
    expect(search(page).get('panel')).not.toContain('@s=')
  })

  test('Show more survives a reload', async ({ page }) => {
    await openOverview(page, '/', addFailures)
    await clickBar(page, 8)
    await expect(panelOf(page).getByTestId('panel-run')).toHaveCount(40)
    await showMore(page).click()
    await expect(panelOf(page).getByTestId('panel-run')).toHaveCount(47)
    expect(search(page).get('panel')).toMatch(/@m=1$/)

    await page.reload()
    await expect(panelOf(page).getByTestId('panel-run')).toHaveCount(47)
  })

  test('chips and Show more do not add history entries', async ({ page }) => {
    await openOverview(page, '/', addFailures)
    await clickBar(page, 8)
    await expect(panelOf(page).getByTestId('panel-run')).toHaveCount(40)
    const before = await historyLength(page)
    await chip(page, 'Failure').click()
    await chip(page, 'Failure').click()
    await showMore(page).click()
    await expect(panelOf(page).getByTestId('panel-run')).toHaveCount(47)
    expect(await historyLength(page)).toBe(before)
  })

  test('a brush survives a reload and one drag adds no history entry', async ({ page }) => {
    await openOverview(page)
    const before = await historyLength(page)
    await brushBars(page, 7, 9)
    await expect(page.getByTestId('brush-label')).toHaveText(/\d\d:\d\d–\d\d:\d\d/)
    const label = await page.getByTestId('brush-label').textContent()
    expect(search(page).get('brush')).toMatch(/^\d+-\d+$/)
    expect(await historyLength(page)).toBe(before)

    await page.reload()
    await expect(page.getByTestId('brush-label')).toHaveText(label ?? '')
    await expect(page.getByTestId('brush')).toHaveCount(1)

    await page.getByRole('button', { name: 'Clear time filter' }).click()
    await expect(page.getByTestId('brush-label')).toHaveCount(0)
    expect(search(page).has('brush')).toBe(false)
    expect(await historyLength(page)).toBe(before)
  })

  test('Back from the runs of a brush keeps the brush and closes the panel', async ({ page }) => {
    await openOverview(page)
    await brushBars(page, 7, 9)
    const label = await page.getByTestId('brush-label').textContent()
    await page.getByRole('button', { name: 'Open runs' }).click()
    await expect(panelOf(page).getByTestId('panel-run')).toHaveCount(2)
    await page.reload()
    await expect(panelOf(page).getByTestId('panel-run')).toHaveCount(2)
    await expect(page.getByTestId('brush')).toHaveAttribute('data-on', '')

    await page.goBack()
    await expect(panelOf(page)).toHaveCount(0)
    await expect(page.getByTestId('brush-label')).toHaveText(label ?? '')
  })

  test('a copied URL opens the same view in a new page', async ({ page, context }) => {
    await openOverview(page)
    await brushBars(page, 7, 9)
    await page.getByRole('button', { name: 'Open runs' }).click()
    await expect(panelOf(page).getByTestId('panel-run')).toHaveCount(2)
    await chip(page, 'Failure').click()
    await expect(panelOf(page).getByTestId('panel-run')).toHaveCount(1)
    const label = await page.getByTestId('brush-label').textContent()
    const url = new URL(page.url())

    const other = await context.newPage()
    await openOverview(other, url.pathname + url.search)
    await expect(other.getByTestId('brush-label')).toHaveText(label ?? '')
    await expect(chip(other, 'Failure')).toHaveAttribute('aria-pressed', 'true')
    await expect(panelOf(other).getByTestId('panel-run')).toHaveCount(1)
  })

  test('a brush outside the range keeps its label, draws no span and lists its own runs', async ({
    page,
  }) => {
    const { scenario } = await openOverview(page)
    const urls: string[] = []
    page.on('request', (r) => {
      if (/\/api\/tasks\?/.test(r.url())) urls.push(r.url())
    })
    const from = Math.floor(scenario.start / 1000) - 7200
    await page.goto(`/?brush=${from}-${from + 900}`)
    await expect(page.getByTestId('brush-label')).toBeVisible()
    await expect(page.getByTestId('brush')).toHaveCount(0)

    await page.getByRole('button', { name: 'Open runs' }).click()
    await expect(panelOf(page).getByText('No runs match.')).toBeVisible()
    await expect(panelOf(page).getByText('Failure rate')).toHaveCount(0)
    expect(urls.some((u) => u.includes('from_date') && u.includes('updated_before'))).toBe(true)
  })

  test('a brush that starts before the range is drawn from the first bar', async ({ page }) => {
    const { scenario } = await openOverview(page)
    const start = Math.floor(scenario.start / 1000)
    await page.goto(`/?brush=${start - 1800}-${start + 900}`)
    await expect(page.getByTestId('brush')).toHaveCount(1)
    const bars = page.getByTestId('chart-bar')
    const first = await bars.nth(0).boundingBox()
    const second = await bars.nth(1).boundingBox()
    const brush = await page.getByTestId('brush').boundingBox()
    if (!first || !second || !brush) throw new Error('chart not drawn')
    const pitch = second.x - first.x
    expect(Math.abs(brush.x - first.x)).toBeLessThan(pitch)
    expect(Math.abs(brush.width - 3 * pitch)).toBeLessThan(2)
  })

  test('changing the range keeps the brush', async ({ page }) => {
    await openOverview(page)
    await brushBars(page, 7, 9)
    const value = search(page).get('brush')
    await page
      .getByRole('group', { name: 'Time range' })
      .getByRole('button', { name: '1h' })
      .click()
    await expect(page.getByTestId('brush-label')).toBeVisible()
    expect(search(page).get('brush')).toBe(value)
    expect(search(page).get('range')).toBe('1h')
  })

  const INVALID = [
    '/?brush=abc',
    '/?brush=5-3',
    '/?brush=1764151500',
    '/?brush=99999999999999999-999999999999999999',
  ]
  for (const path of INVALID) {
    test(`an invalid brush falls back to none: ${path}`, async ({ page }) => {
      await openOverview(page, path)
      await expect(page.getByTestId('brush-label')).toHaveCount(0)
      await expect(page.getByTestId('chart-bar')).toHaveCount(12)
    })
  }

  const barPanel = (scenario: OverviewScenario, view: string): string => {
    const id = encodeScope({
      kind: 'bar',
      from: new Date(bucketStart(scenario, 8)).toISOString(),
      to: new Date(bucketStart(scenario, 9)).toISOString(),
    })
    return `/?panel=${encodeURIComponent(`${formatPanel([{ kind: 'runs', id }])}${view}`)}`
  }

  const addMany: Setup = (api, scenario) => {
    for (let i = 0; i < 540; i++) {
      const task = createFailedTask('tests.flow.step_2', 'ValueError: x', 'Traceback')
      task.last_updated = new Date(bucketStart(scenario, 8) + 60_000 + i * 100).toISOString()
      api.addTask(task)
    }
  }

  for (const more of [12, 13]) {
    test(`Show more at @m=${more} asks for at most 500 runs and says it stops there`, async ({
      page,
    }) => {
      const rejected: string[] = []
      page.on('response', (r) => {
        if (/\/api\/tasks\?/.test(r.url()) && r.status() === 422) rejected.push(r.url())
      })
      const { scenario } = await openOverview(page, '/', addMany)
      await page.goto(barPanel(scenario, `@m=${more}`))
      await expect(panelOf(page).getByTestId('panel-run')).toHaveCount(500)
      await expect(panelOf(page).getByText(/Showing the first 500 of 5\d\d runs/)).toBeVisible()
      await expect(showMore(page)).toHaveCount(0)
      expect(rejected).toEqual([])
    })
  }

  for (const depth of [1, 2, 3]) {
    test(`opening the bottom entry again keeps its chips at depth ${depth}`, async ({ page }) => {
      await openOverview(page)
      const chart = page.locator('[data-testid="runs-chart"] svg')
      // Enter opens the runs of the last bar, which the open panel leaves reachable by keyboard
      await chart.focus()
      await page.keyboard.press('Enter')
      await chip(page, 'Failure').click()
      await expect(chip(page, 'Failure')).toHaveAttribute('aria-pressed', 'true')
      const panelValue = search(page).get('panel') ?? ''
      expect(panelValue).toContain('@s=FAILURE')
      if (depth > 1) {
        // Entries above it, as a click on a run (and a run inside it) pushes them
        const above = Array.from({ length: depth - 1 }, (_, i) => `/task:t${i}`).join('')
        await page.goto(`/?panel=${encodeURIComponent(panelValue + above)}`)
        await expect(panelOf(page)).toBeVisible()
      }
      const before = await historyLength(page)

      await chart.focus()
      await page.keyboard.press('Enter')
      await expect(chip(page, 'Failure')).toHaveAttribute('aria-pressed', 'true')
      expect(search(page).get('panel')).toBe(panelValue)
      expect(await historyLength(page)).toBe(before + (depth > 1 ? 1 : 0))
    })
  }

  test('the Show more label counts only the rows the 500 cap allows', async ({ page }) => {
    const { scenario } = await openOverview(page, '/', addMany)
    await page.goto(barPanel(scenario, '@m=11'))
    await expect(panelOf(page).getByTestId('panel-run')).toHaveCount(480)
    await expect(showMore(page)).toHaveText(/^Show 20 more of 5\d\d$/)
  })

  test('two Show more clicks in one tick stop at the last step', async ({ page }) => {
    const { scenario } = await openOverview(page, '/', addMany)
    await page.goto(barPanel(scenario, '@m=12'))
    await expect(panelOf(page).getByTestId('panel-run')).toHaveCount(500)
    await page.goto(barPanel(scenario, '@m=11'))
    await expect(showMore(page)).toBeVisible()
    await page.evaluate(() => {
      const button = [...document.querySelectorAll('button')].find((b) =>
        /^Show \d+ more/.test(b.textContent ?? ''),
      )
      button?.click()
      button?.click()
    })
    await expect(panelOf(page).getByTestId('panel-run')).toHaveCount(500)
    expect(decodeURIComponent(search(page).get('panel') ?? '')).toMatch(/@m=13$/)
  })

  test('a tile that fixes a state ignores the chips in the link', async ({ page }) => {
    const urls: string[] = []
    page.on('request', (r) => {
      if (/\/api\/tasks\?/.test(r.url())) urls.push(r.url())
    })
    await openOverview(page)
    const id = encodeScope({ kind: 'kpi', state: 'FAILURE', within: '6h', label: 'Failures' })
    await page.goto(
      `/?panel=${encodeURIComponent(`${formatPanel([{ kind: 'runs', id }])}@s=SUCCESS`)}`,
    )
    await expect(panelOf(page).getByTestId('panel-run').first()).toBeVisible()
    await expect(panelOf(page).getByRole('group', { name: 'Filter by state' })).toHaveCount(0)
    expect(urls.length).toBeGreaterThan(0)
    expect(urls.filter((u) => u.includes('SUCCESS'))).toEqual([])
  })

  test('two chips picked in one tick are both kept', async ({ page }) => {
    await openOverview(page)
    await clickBar(page, 8)
    await expect(chip(page, 'Failure')).toBeVisible()
    await page.evaluate(() => {
      const group = document.querySelector('[aria-label="Filter by state"]')
      const press = (name: string) =>
        [...(group?.querySelectorAll('button') ?? [])].find((b) => b.textContent === name)?.click()
      press('Failure')
      press('Retry')
    })
    await expect(chip(page, 'Failure')).toHaveAttribute('aria-pressed', 'true')
    await expect(chip(page, 'Retry')).toHaveAttribute('aria-pressed', 'true')
    expect(search(page).get('panel')).toMatch(/@s=FAILURE,RETRY$/)
  })

  test('invalid chips and Show more fall back to the defaults', async ({ page }) => {
    const { scenario } = await openOverview(page)
    const id = encodeScope({
      kind: 'bar',
      from: new Date(bucketStart(scenario, 8)).toISOString(),
      to: new Date(bucketStart(scenario, 9)).toISOString(),
    })
    const panel = formatPanel([{ kind: 'runs', id }])
    await page.goto(`/?panel=${encodeURIComponent(`${panel}@s=BOGUS,NOPE@m=-4`)}`)
    await expect(chip(page, 'All')).toHaveAttribute('aria-pressed', 'true')
    await expect(panelOf(page).getByTestId('panel-run').first()).toBeVisible()
  })
})
