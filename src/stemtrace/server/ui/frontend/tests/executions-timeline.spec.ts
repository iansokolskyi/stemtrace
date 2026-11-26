import { expect, type Locator, type Page, test } from '@playwright/test'

import { barParts, legendStates, utcLabel } from '../src/components/timelineBars'
import { setupMockApi } from './fixtures/mock-api'
import type { MockTask } from './fixtures/mock-data'
import { run, T0, threeRuns } from './fixtures/timeline'

/**
 * The Executions page's Timeline view: a row per run on one UTC, 24-hour axis, bars in the flow
 * page's language, a legend of the states shown, and the design tokens in both themes. Mock mode.
 */

test.skip(process.env.E2E_MODE === 'real', 'Mock-only test')

// A Tokyo browser: local time would read 08:58, so only a UTC axis passes
test.use({ timezoneId: 'Asia/Tokyo', locale: 'en-US' })

async function openTimeline(
  page: Page,
  tasks: MockTask[],
  theme?: 'light' | 'dark',
): Promise<void> {
  if (theme) await page.addInitScript((t) => localStorage.setItem('stemtrace-theme', t), theme)
  await page.routeWebSocket(/\/ws$/, () => {})
  const api = await setupMockApi(page, { useDefaults: false })
  api.addTasks(tasks)
  await page.goto('/executions?view=timeline')
  await expect(page.getByTestId('timeline-row').first()).toBeVisible()
}

/** `var(--token)` resolved to the rgb() string the browser computes, for a colour or background. */
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

const rowOf = (page: Page, name: string): Locator =>
  page.getByTestId('timeline-row').filter({ has: page.getByRole('link', { name }) })

test.describe('Executions timeline: rows', () => {
  test('lists one row per run with its short name, duration and a link to the run', async ({
    page,
  }) => {
    const tasks = threeRuns()
    await openTimeline(page, tasks)

    await expect(page.getByTestId('timeline-row')).toHaveCount(3)
    const row = rowOf(page, 'shop.orders.reconcile')
    await expect(row).toContainText('reconcile')
    await expect(row).toContainText('5.0s')
    await expect(rowOf(page, 'shop.mail.send_receipt')).toContainText('3.0s')
    await expect(page.getByRole('link', { name: 'shop.sync.pull' })).toHaveAttribute(
      'href',
      `/tasks/${tasks[2].task_id}`,
    )

    await row.getByRole('link').click()
    await expect(page).toHaveURL(new RegExp(`/tasks/${tasks[0].task_id}$`))
  })

  test('draws the wait, the earlier attempts and the last run as separate parts', async ({
    page,
  }) => {
    await openTimeline(page, threeRuns())

    const parts = async (name: string) =>
      rowOf(page, name)
        .locator('[data-part]')
        .evaluateAll((els) => els.map((el) => el.getAttribute('data-part')))
    expect(await parts('shop.orders.reconcile')).toEqual(['queued', 'run'])
    expect(await parts('shop.sync.pull')).toEqual(['queued', 'tries', 'run'])
  })

  test('places each bar by its time on the shared axis', async ({ page }) => {
    await openTimeline(page, threeRuns())

    // Axis 0..8 s: the failure's run starts at 3 s and ends at 4 s
    const plot = await rowOf(page, 'shop.mail.send_receipt')
      .locator('[data-part="run"]')
      .evaluate((el) => {
        const parent = el.parentElement as HTMLElement
        const bar = el.getBoundingClientRect()
        const box = parent.getBoundingClientRect()
        return { left: (bar.left - box.left) / box.width, width: bar.width / box.width }
      })
    expect(plot.left).toBeCloseTo(3 / 8, 2)
    expect(plot.width).toBeCloseTo(1 / 8, 2)
  })

  test('shows no rows for runs without timing, and says so', async ({ page }) => {
    const bare = run('dddd0000-0000-4000-8000-000000000004', 'shop.bare', 'PENDING', [
      { state: 'PENDING', s: 0 },
    ])
    bare.first_seen = null
    bare.last_updated = null
    await page.routeWebSocket(/\/ws$/, () => {})
    const api = await setupMockApi(page, { useDefaults: false })
    api.addTasks([bare])
    await page.goto('/executions?view=timeline')

    await expect(page.getByText('No timing data available')).toBeVisible()
  })

  test('says so when the request fails', async ({ page }) => {
    await page.routeWebSocket(/\/ws$/, () => {})
    await setupMockApi(page, { useDefaults: false })
    await page.route(/\/api\/tasks(\?|$)/, (route) =>
      route.fulfill({ status: 500, json: { detail: 'boom' } }),
    )
    await page.goto('/executions?view=timeline')

    await expect(page.getByText('Failed to load timeline')).toBeVisible({ timeout: 15_000 })
  })
})

test.describe('Executions timeline: legend', () => {
  test('lists only the states shown, in the words the rest of the UI uses', async ({ page }) => {
    await openTimeline(page, threeRuns())

    await expect(page.getByTestId('timeline-legend-item')).toHaveText([
      'Success',
      'Failure',
      'Retry',
    ])
  })

  test('has no entry for a state no row is in', async ({ page }) => {
    await openTimeline(page, threeRuns().slice(0, 1))

    await expect(page.getByTestId('timeline-legend-item')).toHaveText(['Success'])
  })

  test('orders and de-duplicates the states it is given', () => {
    expect(legendStates(['PENDING', 'FAILURE', 'SUCCESS', 'FAILURE', 'ZED', 'ALPHA'])).toEqual([
      'SUCCESS',
      'FAILURE',
      'PENDING',
      'ALPHA',
      'ZED',
    ])
  })
})

test.describe('Executions timeline: UTC 24-hour axis', () => {
  test('labels the axis in UTC, from a Tokyo browser, with no AM or PM', async ({ page }) => {
    await openTimeline(page, threeRuns())

    await expect(page.getByTestId('timeline-axis').locator('span')).toHaveText([
      '23:58:00',
      '23:58:02',
      '23:58:04',
      '23:58:06',
      '23:58:08',
    ])
  })

  test('adds the date when the axis spans a day or more', async ({ page }) => {
    const old = run('eeee0000-0000-4000-8000-000000000005', 'shop.old', 'SUCCESS', [
      { state: 'STARTED', s: -90_000 },
      { state: 'SUCCESS', s: -89_990 },
    ])
    await openTimeline(page, [...threeRuns(), old])

    const labels = await page.getByTestId('timeline-axis').locator('span').allTextContents()
    expect(labels[0]).toBe('10-16 22:58')
    expect(labels[4]).toBe('10-17 23:58')
  })

  test('formats a label from the epoch, not the viewer clock', () => {
    expect(utcLabel(Date.parse('2026-01-02T03:04:05Z'), 1000)).toBe('03:04:05')
    expect(utcLabel(Date.parse('2026-01-02T15:04:05Z'), 86_400_000)).toBe('01-02 15:04')
  })

  test('tooltip gives the state, the time spent and the UTC start', async ({ page }) => {
    await openTimeline(page, threeRuns())

    const bar = rowOf(page, 'shop.mail.send_receipt').locator('[data-part="run"] [data-fill]')
    await bar.hover()
    const tip = rowOf(page, 'shop.mail.send_receipt').getByText('Failure · 1.0s · 23:58:03 UTC')
    await expect(tip).toBeVisible()
    await expect(tip).toHaveCSS('border-top-left-radius', '3px')
    await expect(tip).toHaveCSS('background-color', await token(page, 'raised'))
  })
})

test.describe('barParts', () => {
  test('a run that never started is one run part', () => {
    const [task] = threeRuns()
    const pending = { ...task, state: 'PENDING', events: task.events.slice(0, 1) }
    expect(barParts(pending).map((p) => p.kind)).toEqual(['run'])
  })

  test('a run without events falls back to first_seen to last_updated', () => {
    const [task] = threeRuns()
    expect(barParts({ ...task, events: [] })).toEqual([{ kind: 'run', from: T0, to: T0 + 5000 }])
  })
})

for (const theme of ['light', 'dark'] as const) {
  test.describe(`Executions timeline: tokens, ${theme}`, () => {
    test('the card is the surface token with the card radius, bars use the state tokens', async ({
      page,
    }) => {
      await openTimeline(page, threeRuns(), theme)

      const card = page.getByRole('region', { name: 'Timeline' })
      await expect(card).toHaveCSS('background-color', await token(page, 'surface'))
      await expect(card).toHaveCSS('border-top-left-radius', '8px')
      const fill = (name: string) =>
        rowOf(page, name).locator('[data-part="run"] [data-fill]').first()
      await expect(fill('shop.orders.reconcile')).toHaveCSS(
        'background-color',
        await token(page, 'success'),
      )
      await expect(fill('shop.mail.send_receipt')).toHaveCSS(
        'background-color',
        await token(page, 'failure'),
      )
      // Retry is hatched with the retry token, as everywhere else
      const hatch = await fill('shop.sync.pull').evaluate(
        (el) => getComputedStyle(el).backgroundImage,
      )
      expect(hatch).toContain('repeating-linear-gradient')
    })

    test('uses no colour outside the tokens: no slate, emerald or navy fills', async ({ page }) => {
      await openTimeline(page, threeRuns(), theme)

      const classes = await page
        .getByRole('region', { name: 'Timeline' })
        .evaluate((el) =>
          [el, ...el.querySelectorAll('*')].map((node) => node.getAttribute('class') ?? ''),
        )
      expect(classes.join(' ')).not.toMatch(/\b(?:bg|text|border)-(?:slate|emerald|zinc|gray)-/)
    })
  })
}
