import { expect, type Locator, type Page, test } from '@playwright/test'

import { setupMockApi } from './fixtures/mock-api'
import { createFailedTask, createSuccessTask, type MockTask } from './fixtures/mock-data'

/**
 * StatusChip and TaskName, shown through the Executions page (the first page that uses them).
 * Mock mode only: it controls the theme and the API.
 */

test.skip(process.env.E2E_MODE === 'real', 'Mock-only test')

function retryTask(name: string): MockTask {
  const task = createSuccessTask(name)
  task.state = 'RETRY'
  return task
}

/** Resolves `var(--token)` through a probe, so both sides of a comparison are rgb() strings. */
async function tokenColour(page: Page, token: string): Promise<string> {
  return page.evaluate((name) => {
    const probe = document.createElement('div')
    probe.style.color = `var(--${name})`
    document.body.append(probe)
    const colour = getComputedStyle(probe).color
    probe.remove()
    return colour
  }, token)
}

const chip = (page: Page, state: string): Locator => page.locator(`[data-state="${state}"]`).first()

for (const theme of ['light', 'dark'] as const) {
  test.describe(`StatusChip, ${theme}`, () => {
    test.beforeEach(async ({ page }) => {
      await page.addInitScript((t) => localStorage.setItem('stemtrace-theme', t), theme)
      const api = await setupMockApi(page, { useDefaults: false })
      api.addTasks([
        createSuccessTask('app.ok'),
        createFailedTask('app.bad', 'ValueError: boom', 'Traceback...'),
        retryTask('app.again'),
      ])
      await page.goto('/executions')
      await expect(page.locator('[data-state]').first()).toBeVisible()
    })

    test('failure is loud: ink bold word and a filled failure block', async ({ page }) => {
      const failure = chip(page, 'FAILURE')
      await expect(failure).toHaveAttribute('data-variant', 'loud')
      const block = failure.locator('[data-status-icon]')
      await expect(block).toHaveCSS('background-color', await tokenColour(page, 'failure'))
      await expect(block).toHaveCSS('color', await tokenColour(page, 'on-failure'))
      await expect(failure).toHaveCSS('color', await tokenColour(page, 'ink'))
      await expect(failure).toHaveCSS('font-weight', '700')
    })

    test('success is quiet: muted regular word and no filled block', async ({ page }) => {
      const success = chip(page, 'SUCCESS')
      await expect(success).toHaveAttribute('data-variant', 'quiet')
      await expect(success).toHaveCSS('color', await tokenColour(page, 'muted'))
      await expect(success).toHaveCSS('font-weight', '400')
      await expect(success.locator('[data-status-icon]')).toHaveCSS(
        'background-color',
        'rgba(0, 0, 0, 0)',
      )
    })

    test('retry is hatched with the retry token and surface gaps', async ({ page }) => {
      const block = chip(page, 'RETRY').locator('[data-status-icon]')
      const image = await block.evaluate((el) => getComputedStyle(el).backgroundImage)
      expect(image).toContain('repeating-linear-gradient(45deg')
      expect(image).toContain(await tokenColour(page, 'retry'))
      expect(image).toContain(await tokenColour(page, 'surface'))
    })

    test('the accessible name carries the state word, not just the colour', async ({ page }) => {
      await expect(page.getByRole('link', { name: /Failure/ })).toHaveCount(1)
      await expect(page.getByRole('link', { name: /Success/ })).toHaveCount(1)
      await expect(page.getByRole('link', { name: /Retry/ })).toHaveCount(1)
      // The failure's exception type is on hover
      await expect(chip(page, 'FAILURE')).toHaveAttribute('title', 'ValueError')
    })
  })
}

test.describe('TaskName', () => {
  const LONG = 'billing.invoices.tasks.reminders.overdue.send_reminder'

  test('module is muted and the last segment is semibold, text is the full name', async ({
    page,
  }) => {
    const api = await setupMockApi(page, { useDefaults: false })
    api.addTask(createSuccessTask('app.billing.send'))
    await page.goto('/executions')
    const name = page.getByText('app.billing.send', { exact: true })
    await expect(name).toHaveAttribute('title', 'app.billing.send')
    await expect(name).toHaveCSS('font-family', /Overpass Mono Variable/)
    await expect(name.locator('[data-task-module]')).toHaveText('app.billing.')
    await expect(name.locator('[data-task-module]')).toHaveCSS(
      'color',
      await tokenColour(page, 'muted'),
    )
    await expect(name.locator('[data-task-last]')).toHaveText('send')
    await expect(name.locator('[data-task-last]')).toHaveCSS('font-weight', '600')
  })

  test('at 400px a long name keeps its last segment whole and cuts the module', async ({
    page,
  }) => {
    await page.setViewportSize({ width: 400, height: 800 })
    const api = await setupMockApi(page, { useDefaults: false })
    api.addTask(createSuccessTask(LONG))
    await page.goto('/executions')
    const name = page.getByText(LONG, { exact: true })
    await expect(name).toBeVisible()
    const last = name.locator('[data-task-last]')
    const module = name.locator('[data-task-module]')
    const fits = (el: Locator) => el.evaluate((e) => e.scrollWidth <= e.clientWidth)
    expect(await fits(last)).toBe(true)
    expect(await fits(module)).toBe(false)
    // The segment is inside the viewport, not pushed past its right edge
    const box = await last.boundingBox()
    expect(box).not.toBeNull()
    expect((box?.x ?? 0) + (box?.width ?? 0)).toBeLessThanOrEqual(400)
  })
})
