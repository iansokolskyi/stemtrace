import { expect, type Locator, type Page, test } from '@playwright/test'

import { formatPanel } from '../src/components/triage/panelState'
import { encodeScope } from '../src/components/triage/runsScope'
import { type MockApiContext, setupMockApi } from './fixtures/mock-api'
import { createFailedTask, createSuccessTask, type MockTask } from './fixtures/mock-data'
import {
  mockOverview,
  type OverviewScenario,
  type Percentile,
  spikeScenario,
} from './fixtures/overview'

/**
 * The p95 runtime and p95 queue wait tiles (U1, spec `overview-p95-and-polish.md` section 1 and
 * the owner decisions of 2026-11-26). Mock mode only: the numbers come from `fixtures/overview.ts`
 * and the sorted run list from the mock's `sort` (which models stats.py `*_sort_value`).
 */

const isRealMode = process.env.E2E_MODE === 'real'
const MIN = 60_000

const pct = (
  value: number | null,
  baseline: number | null,
  samples = 480,
  baselineSamples = 470,
): Percentile => ({ value, baseline, samples, baseline_samples: baselineSamples })

interface Opened {
  api: MockApiContext
  scenario: OverviewScenario
  requests: URL[]
}

async function openOverview(
  page: Page,
  shape?: (scenario: OverviewScenario, api: MockApiContext) => void,
  path = '/',
): Promise<Opened> {
  const api = await setupMockApi(page)
  api.clearTasks()
  const scenario = spikeScenario()
  shape?.(scenario, api)
  const requests = await mockOverview(page, scenario)
  await page.goto(path)
  await expect(page.getByRole('heading', { level: 1, name: 'Overview' })).toBeVisible()
  return { api, scenario, requests }
}

const tilesOf = (page: Page): Locator => page.getByTestId('kpi-tiles')
const tile = (page: Page, name: RegExp): Locator => tilesOf(page).getByRole('button', { name })
const runtimeTile = (page: Page): Locator => tile(page, /^p95 runtime/)
const waitTile = (page: Page): Locator => tile(page, /^p95 queue wait/)

/** The scenario's p95 block; a test that shapes it has one. */
function p95Of(scenario: OverviewScenario): NonNullable<OverviewScenario['p95']> {
  if (!scenario.p95) throw new Error('scenario has no p95')
  return scenario.p95
}

/** One run for the panel, with the fields the sort reads. */
function run(
  name: string,
  state: string,
  fields: { runtime?: number | null; wait?: number | null; agoMs?: number; id?: string },
): MockTask {
  const task =
    state === 'FAILURE' ? createFailedTask(name, 'ValueError: x', 'tb') : createSuccessTask(name)
  task.state = state
  task.runtime_ms = fields.runtime ?? null
  task.queue_wait_ms = fields.wait ?? null
  task.last_updated = new Date(Date.now() - (fields.agoMs ?? MIN)).toISOString()
  if (fields.id) task.task_id = fields.id
  return task
}

const panelRows = (page: Page): Locator => page.getByTestId('panel-run')
const rowIds = (page: Page): Promise<string[]> =>
  panelRows(page).evaluateAll((rows) => rows.map((r) => r.getAttribute('data-task-id') ?? ''))
const id = (n: number): string => `aaaa000${n}-0000-0000-0000-00000000000${n}`

test.describe('p95 tiles', () => {
  test.skip(isRealMode, 'Mock-only test')

  test('the row has six tiles in order and no Failures tile', async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 900 })
    await openOverview(page)
    const tiles = tilesOf(page).locator('> *')
    await expect(tiles).toHaveCount(6)
    const labels = await tiles.evaluateAll((els) =>
      els.map((el) => el.querySelector('span')?.textContent ?? ''),
    )
    expect(labels).toEqual([
      'Runs / min',
      'Failure rate',
      'p95 runtime',
      'p95 queue wait',
      'Workers online',
      'Anomalies',
    ])
    await expect(tilesOf(page).getByText('Failures', { exact: true })).toHaveCount(0)
  })

  for (const [width, perRow] of [
    [1280, 6],
    [1000, 3],
    [400, 2],
  ] as const) {
    test(`at ${width}px the six tiles fill rows of ${perRow} with equal widths`, async ({
      page,
    }) => {
      await page.setViewportSize({ width, height: 900 })
      await openOverview(page)
      const boxes = await tilesOf(page)
        .locator('> *')
        .evaluateAll((els) =>
          els.map((el) => {
            const r = el.getBoundingClientRect()
            return { y: Math.round(r.y), w: Math.round(r.width) }
          }),
        )
      expect(boxes).toHaveLength(6)
      const rows = new Set(boxes.map((b) => b.y))
      expect(rows.size).toBe(6 / perRow)
      for (const b of boxes) expect(Math.abs(b.w - boxes[0].w)).toBeLessThanOrEqual(1)
      // Every row is full: no tile alone on a line
      for (const y of rows) expect(boxes.filter((b) => b.y === y)).toHaveLength(perRow)
    })
  }

  test('the p95 runtime tile shows the value, the note, the earlier value and a sparkline', async ({
    page,
  }) => {
    const { requests } = await openOverview(page)
    const t = runtimeTile(page)
    await expect(t).toContainText('1.7s')
    // 1680 / 1200 = 1.4 exactly: the warn threshold
    await expect(t).toContainText('Above baseline: +40%')
    await expect(t).toContainText('earlier: 1.2s')
    await expect(t).toHaveAttribute('data-grade', 'warn')
    await expect(t.locator('polyline')).toHaveCount(1)
    const points = await t.locator('polyline').getAttribute('points')
    expect(points?.split(' ')).toHaveLength(12)
    // The series is asked for with the p95, over the same window as the chart
    expect(
      requests.some(
        (u) =>
          u.pathname.endsWith('/stats/timeseries') && u.searchParams.get('percentiles') === 'p95',
      ),
    ).toBe(true)
  })

  test('the p95 queue wait tile reads its own field and says In baseline within it', async ({
    page,
  }) => {
    await openOverview(page)
    const t = waitTile(page)
    await expect(t).toContainText('340ms')
    await expect(t).toContainText('In baseline')
    await expect(t).toContainText('earlier: 280ms')
    await expect(t).toHaveAttribute('data-grade', 'ok')
  })

  // The prototype's thresholds: runtime warn 1.4x, bad 2.5x; wait warn 1.5x, bad 3x; a rise only
  const THRESHOLDS = [
    ['runtime', 1390, 'ok'],
    ['runtime', 1400, 'warn'],
    ['runtime', 2490, 'warn'],
    ['runtime', 2500, 'bad'],
    ['runtime', 400, 'ok'],
    ['queue_wait', 1490, 'ok'],
    ['queue_wait', 1500, 'warn'],
    ['queue_wait', 2990, 'warn'],
    ['queue_wait', 3000, 'bad'],
    ['queue_wait', 300, 'ok'],
  ] as const
  for (const [which, value, grade] of THRESHOLDS) {
    test(`p95 ${which} ${value} against 1000 is ${grade}`, async ({ page }) => {
      await openOverview(page, (s) => {
        p95Of(s)[which] = pct(value, 1000)
      })
      const t = which === 'runtime' ? runtimeTile(page) : waitTile(page)
      await expect(t).toHaveAttribute('data-grade', grade)
      if (grade === 'bad') await expect(t).toContainText('Well above baseline')
    })
  }

  // Q2: a grade needs at least 20 samples in both windows; the value always shows
  const GATE = [
    [20, 20, true],
    [19, 470, false],
    [480, 19, false],
    [19, 19, false],
  ] as const
  for (const [samples, baselineSamples, graded] of GATE) {
    test(`with ${samples} samples now and ${baselineSamples} before the tile ${graded ? 'grades' : 'shows the value with no grade'}`, async ({
      page,
    }) => {
      await openOverview(page, (s) => {
        // 5x: far above the baseline, so a grade would be `bad`
        p95Of(s).runtime = pct(5000, 1000, samples, baselineSamples)
      })
      const t = runtimeTile(page)
      await expect(t).toContainText('5.0s')
      if (graded) {
        await expect(t).toHaveAttribute('data-grade', 'bad')
        await expect(t).toContainText('earlier: 1.0s')
      } else {
        await expect(t).toHaveAttribute('data-grade', 'ok')
        await expect(t).not.toContainText('baseline')
        await expect(t).toContainText(`from ${samples} runs`)
        await expect(t).not.toContainText('earlier')
      }
    })
  }

  test('a single sample says "from 1 run"', async ({ page }) => {
    await openOverview(page, (s) => {
      p95Of(s).runtime = pct(900, null, 1, 0)
    })
    await expect(runtimeTile(page)).toContainText('from 1 run')
    await expect(runtimeTile(page)).not.toContainText('from 1 runs')
  })

  test('with range All the value shows and nothing is compared', async ({ page }) => {
    await openOverview(page, undefined, '/?range=all')
    const t = runtimeTile(page)
    await expect(t).toContainText('1.7s')
    await expect(t).toHaveAttribute('data-grade', 'ok')
    await expect(t).not.toContainText('baseline')
    await expect(t).not.toContainText('earlier')
    await expect(t).toContainText('from 480 runs')
  })

  test('with no samples the tiles say No data and keep their size', async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 900 })
    await openOverview(page, (s) => {
      const p95 = p95Of(s)
      p95.runtime = pct(null, null, 0, 0)
      p95.queue_wait = pct(null, null, 0, 0)
      p95.runtime_series = p95.runtime_series.map(() => null)
      p95.queue_wait_series = p95.queue_wait_series.map(() => null)
    })
    await expect(runtimeTile(page)).toContainText('No data')
    await expect(runtimeTile(page)).toContainText('No finished runs with a runtime')
    await expect(waitTile(page)).toContainText('No data')
    await expect(waitTile(page)).toContainText('No runs with a measured wait')
    await expect(runtimeTile(page).locator('polyline')).toHaveCount(0)
    const heights = await tilesOf(page)
      .locator('> *')
      .evaluateAll((els) => els.map((el) => Math.round(el.getBoundingClientRect().height)))
    expect(new Set(heights).size).toBe(1)
  })

  test('a bucket with no sample breaks the line there: two segments, never a bridge or a zero', async ({
    page,
  }) => {
    await openOverview(page, (s) => {
      const p95 = p95Of(s)
      p95.runtime_series = p95.runtime_series.map((v, i) => (i === 4 ? null : v))
    })
    const lines = runtimeTile(page).locator('polyline')
    await expect(lines).toHaveCount(2)
    const sizes = await lines.evaluateAll((els) =>
      els.map((el) => (el.getAttribute('points') ?? '').split(' ').length),
    )
    // Buckets 0-3 and 5-11
    expect(sizes).toEqual([4, 7])
  })

  test('a lone bucket with a sample draws a dot', async ({ page }) => {
    await openOverview(page, (s) => {
      const p95 = p95Of(s)
      p95.runtime_series = p95.runtime_series.map((v, i) => (i === 5 ? v : null))
    })
    await expect(runtimeTile(page).locator('circle')).toHaveCount(1)
    await expect(runtimeTile(page).locator('polyline')).toHaveCount(0)
  })

  // Rows of #324 round 1: a grade needs a baseline over 0 and a rise of at least 50 ms
  const FLOOR = [
    [0, 3, 'ok'],
    [1, 3, 'ok'],
    [0, 0, 'ok'],
    [0, 80, 'ok'],
    [100, 160, 'warn'],
    [20, 60, 'ok'],
    [100, 149, 'ok'],
  ] as const
  for (const [baseline, value, grade] of FLOOR) {
    test(`p95 runtime ${value}ms against ${baseline}ms is ${grade}`, async ({ page }) => {
      await openOverview(page, (s) => {
        p95Of(s).runtime = pct(value, baseline)
      })
      const t = runtimeTile(page)
      await expect(t).toHaveAttribute('data-grade', grade)
      await expect(t).toContainText(`earlier: ${baseline}ms`)
      if (grade === 'ok' && value >= baseline) await expect(t).not.toContainText('baseline')
      if (grade === 'warn') await expect(t).toContainText('Above baseline')
    })
  }

  test('a tile flagged below its baseline reads "Below baseline: -N%"', async ({ page }) => {
    await openOverview(page, (s) => {
      // 5.5 / 8 = 0.69: a fall of 31%, past the 1.4 factor either way
      s.overview.runs_per_minute = [5.5, 8]
    })
    await expect(tile(page, /^Runs \/ min/)).toContainText('Below baseline: \u221231%')
  })

  test('the tile shows the server window value, not a mean of the bucket p95s', async ({
    page,
  }) => {
    await openOverview(page, (s) => {
      const p95 = p95Of(s)
      p95.runtime = pct(9000, 1000)
      p95.runtime_series = Array.from({ length: 12 }, () => 100)
    })
    await expect(runtimeTile(page)).toContainText('9.0s')
  })

  test.describe('an older server without the p95 fields', () => {
    for (const [width, rows] of [
      [1280, 1],
      [1000, 2],
      [400, 2],
    ] as const) {
      test(`hides both tiles and keeps four even tiles at ${width}px`, async ({ page }) => {
        await page.setViewportSize({ width, height: 900 })
        await openOverview(page, (s) => {
          s.p95 = undefined
        })
        const tiles = tilesOf(page).locator('> *')
        await expect(tiles).toHaveCount(4)
        await expect(tilesOf(page).getByText(/p95/)).toHaveCount(0)
        await expect(tilesOf(page).getByText('No data')).toHaveCount(0)
        const boxes = await tiles.evaluateAll((els) =>
          els.map((el) => {
            const r = el.getBoundingClientRect()
            return { y: Math.round(r.y), w: Math.round(r.width) }
          }),
        )
        expect(new Set(boxes.map((b) => b.y)).size).toBe(rows)
        for (const b of boxes) expect(Math.abs(b.w - boxes[0].w)).toBeLessThanOrEqual(1)
      })
    }

    test('a series without p95 arrays leaves the tile without a sparkline, not a dash', async ({
      page,
    }) => {
      await openOverview(page, (s) => {
        p95Of(s).series_has_p95 = false
      })
      await expect(runtimeTile(page)).toContainText('1.7s')
      await expect(runtimeTile(page).locator('polyline')).toHaveCount(0)
    })
  })
})

test.describe('p95 tile click: the slowest runs first', () => {
  test.skip(isRealMode, 'Mock-only test')

  /** Runs with distinct keys, plus the ones the tiles leave out. */
  const buildRuns = (): MockTask[] => [
    run('tests.a.fast', 'SUCCESS', { runtime: 100, wait: 900, id: id(1) }),
    run('tests.a.slow', 'FAILURE', { runtime: 9000, wait: 50, id: id(2) }),
    run('tests.a.mid', 'SUCCESS', { runtime: 3000, wait: 400, id: id(3) }),
    // Revoked after it started: it waited (a wait sample) and has no runtime sample
    run('tests.a.revoked', 'REVOKED', { runtime: 4000, wait: 700, id: id(4) }),
    // Still running: a live wait, no sample (S2c)
    run('tests.a.running', 'STARTED', { runtime: null, wait: 5000, id: id(5) }),
    // Internal tasks are left out of both tiles
    run('celery.chord_unlock', 'SUCCESS', { runtime: 20000, wait: 8000, id: id(6) }),
  ]

  const open = (page: Page) => openOverview(page, (_, api) => api.addTasks(buildRuns()))
  const uncounted = (page: Page, index: number): Locator =>
    panelRows(page).nth(index).getByTestId('panel-run-uncounted')

  test('the runtime tile lists the slowest runs first and marks the runs it does not count', async ({
    page,
  }) => {
    await open(page)
    await runtimeTile(page).click()
    await expect(runtimeTile(page)).toHaveAttribute('data-on', '')
    await expect(panelRows(page)).toHaveCount(6)
    // Keyed runs by runtime; then the keyless ones, newest first, then task_id
    expect(await rowIds(page)).toEqual([id(2), id(3), id(1), id(4), id(5), id(6)])
    for (const i of [0, 1, 2]) await expect(uncounted(page, i)).toHaveCount(0)
    for (const i of [3, 4, 5])
      await expect(uncounted(page, i)).toHaveText('Not counted in the p95 runtime tile')
    // The Executions page has no slowest-first order, so the panel doesn't link to it
    await expect(page.getByRole('link', { name: 'Open in Executions' })).toHaveCount(0)
  })

  test('runs the tiles leave out are marked: rejected, ignored, and a success with no runtime', async ({
    page,
  }) => {
    await openOverview(page, (_, api) =>
      api.addTasks([
        run('tests.c.ok', 'SUCCESS', { runtime: 900, wait: 90, id: id(1) }),
        run('tests.c.rejected', 'REJECTED', { runtime: 800, wait: 80, id: id(2) }),
        run('tests.c.ignored', 'IGNORED', { runtime: 700, wait: 70, id: id(3) }),
        run('tests.c.noruntime', 'SUCCESS', { runtime: null, wait: 60, id: id(4) }),
      ]),
    )
    await runtimeTile(page).click()
    await expect(panelRows(page)).toHaveCount(4)
    const marked = async (): Promise<string[]> =>
      panelRows(page).evaluateAll((rows) =>
        rows
          .filter((r) => r.querySelector('[data-testid="panel-run-uncounted"]'))
          .map((r) => r.getAttribute('data-task-id') ?? '')
          .sort(),
      )
    // Runtime counts SUCCESS and FAILURE with a runtime only
    expect(await marked()).toEqual([id(2), id(3), id(4)])
    await page.keyboard.press('Escape')
    await waitTile(page).click()
    await expect(panelRows(page)).toHaveCount(4)
    // The wait counts any outcome with a wait: only IGNORED is left out
    expect(await marked()).toEqual([id(3)])
  })

  test('the runtime tile asks the tasks API for sort=runtime over its window', async ({ page }) => {
    await open(page)
    const asked = page.waitForRequest((r) => {
      const u = new URL(r.url())
      return u.pathname.endsWith('/api/tasks') && u.searchParams.get('sort') === 'runtime'
    })
    await runtimeTile(page).click()
    const url = new URL((await asked).url())
    expect(url.searchParams.get('within')).toBe('6h')
    expect(url.searchParams.get('offset')).toBe('0')
    expect(url.searchParams.get('limit')).toBe('40')
  })

  test('the queue wait tile asks for sort=queue_wait and counts a revoked run that waited', async ({
    page,
  }) => {
    await open(page)
    const asked = page.waitForRequest((r) => {
      const u = new URL(r.url())
      return u.pathname.endsWith('/api/tasks') && u.searchParams.get('sort') === 'queue_wait'
    })
    await waitTile(page).click()
    await asked
    await expect(panelRows(page)).toHaveCount(6)
    // The running task shows a live wait of 5s but is keyless: after every keyed run
    expect(await rowIds(page)).toEqual([id(1), id(4), id(3), id(2), id(5), id(6)])
    for (const i of [0, 1, 2, 3]) await expect(uncounted(page, i)).toHaveCount(0)
    await expect(panelRows(page).nth(4)).toContainText('5.0s')
    await expect(uncounted(page, 4)).toHaveText('Not counted in the p95 queue wait tile')
    await expect(uncounted(page, 5)).toHaveCount(1)
  })

  test('the panel names the order, and the link is in the URL so a reload restores it', async ({
    page,
  }) => {
    await open(page)
    await runtimeTile(page).click()
    await expect(page.getByTestId('triage-panel')).toContainText('Slowest runtime first')
    // The value the app reads: the URL's raw parameter decoded once, as the router does
    const raw = /[?&]panel=([^&]*)/.exec(new URL(page.url()).search)?.[1] ?? ''
    const panel = decodeURIComponent(raw)
    expect(panel).toBe(
      formatPanel([
        {
          kind: 'runs',
          id: encodeScope({ kind: 'kpi', within: '6h', label: 'Slowest runs', sort: 'runtime' }),
        },
      ]),
    )
    await page.reload()
    await expect(panelRows(page)).toHaveCount(6)
    expect((await rowIds(page))[0]).toBe(id(2))
  })

  test('another tile sends no sort', async ({ page }) => {
    await open(page)
    const seen: string[] = []
    page.on('request', (r) => {
      const u = new URL(r.url())
      if (u.pathname.endsWith('/api/tasks')) seen.push(u.search)
    })
    await tile(page, /^Failure rate/).click()
    await expect.poll(() => seen.length).toBeGreaterThan(0)
    expect(seen.filter((s) => s.includes('sort='))).toEqual([])
  })

  test('Show more grows one request: same sort, offset 0, no second page', async ({ page }) => {
    await openOverview(page, (_, api) => {
      api.addTasks(
        Array.from({ length: 100 }, (_x, i) =>
          run('tests.b.run', 'SUCCESS', {
            runtime: 1000 + i,
            id: `bbbb${String(i).padStart(4, '0')}-0000-0000-0000-000000000000`,
          }),
        ),
      )
    })
    const seen: URL[] = []
    page.on('request', (r) => {
      const u = new URL(r.url())
      if (u.pathname.endsWith('/api/tasks')) seen.push(u)
    })
    await runtimeTile(page).click()
    await expect(panelRows(page)).toHaveCount(40)
    const first = await rowIds(page)
    // Slowest first: the largest runtime is the last id
    expect(first[0]).toBe('bbbb0099-0000-0000-0000-000000000000')
    await page.getByRole('button', { name: /Show 40 more of 100/ }).click()
    await expect(panelRows(page)).toHaveCount(80)
    const grown = await rowIds(page)
    expect(grown.slice(0, 40)).toEqual(first)
    expect(grown[79]).toBe('bbbb0020-0000-0000-0000-000000000000')
    const asks = seen.map((u) => [
      u.searchParams.get('sort'),
      u.searchParams.get('limit'),
      u.searchParams.get('offset'),
    ])
    expect(asks).toContainEqual(['runtime', '40', '0'])
    expect(asks).toContainEqual(['runtime', '80', '0'])
    expect(seen.filter((u) => u.searchParams.get('offset') !== '0')).toEqual([])
  })
})
