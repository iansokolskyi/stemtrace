import { expect, type Locator, type Page, test } from '@playwright/test'

import { setupMockApi } from './fixtures/mock-api'
import {
  createFailedTask,
  createMockEvent,
  createSuccessTask,
  type MockTask,
} from './fixtures/mock-data'

/**
 * The task page, piece by piece: header, timing, facts, arguments, exception, history, children.
 * Mock mode only: it controls which optional fields the API sends.
 */

test.skip(process.env.E2E_MODE === 'real', 'Mock-only test')

const BASE = Date.parse('2026-10-17T12:00:00Z')
const at = (seconds: number): string => new Date(BASE + seconds * 1000).toISOString()

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

/** A successful task with fixed times, plus any optional fields a test wants. */
function successTask(overrides: Partial<MockTask> = {}): MockTask {
  const task = createSuccessTask('billing.invoices.send_receipt', { sent: true })
  const { task_id, name } = task
  task.events = [
    createMockEvent(task_id, name, 'PENDING', { timestamp: at(0), args: [7], root_id: task_id }),
    createMockEvent(task_id, name, 'STARTED', { timestamp: at(1), root_id: task_id }),
    createMockEvent(task_id, name, 'SUCCESS', {
      timestamp: at(2),
      result: { sent: true },
      root_id: task_id,
    }),
  ]
  task.first_seen = at(0)
  task.last_updated = at(2)
  task.duration_ms = 2000
  return Object.assign(task, overrides)
}

async function openTask(page: Page, ...tasks: MockTask[]): Promise<MockTask> {
  const api = await setupMockApi(page, { useDefaults: false })
  api.addTasks(tasks)
  await page.goto(`/tasks/${tasks[0].task_id}`)
  await expect(page.getByRole('heading', { level: 1 })).toBeVisible()
  return tasks[0]
}

const header = (page: Page): Locator => page.getByTestId('task-header')

test.describe('TaskHeader', () => {
  test('the title is the last name segment, the module path is muted above it', async ({
    page,
  }) => {
    const task = await openTask(page, successTask())
    const title = page.getByRole('heading', { level: 1 })
    await expect(title).toHaveText('send_receipt')
    const module = header(page).locator('[data-task-module]')
    await expect(module).toHaveText('billing.invoices')
    await expect(module).toHaveAttribute('title', task.name)
    await expect(module).toHaveCSS('color', await tokenColour(page, 'muted'))
    const moduleBox = await module.boundingBox()
    const titleBox = await title.boundingBox()
    expect((moduleBox?.y ?? 0) + (moduleBox?.height ?? 0)).toBeLessThanOrEqual(titleBox?.y ?? 0)
  })

  test('a name without a module has no module line', async ({ page }) => {
    await openTask(page, successTask({ name: 'cleanup' }))
    await expect(page.getByRole('heading', { level: 1 })).toHaveText('cleanup')
    await expect(header(page).locator('[data-task-module]')).toHaveCount(0)
  })

  test('the state chip and the full ID share one line', async ({ page }) => {
    const task = await openTask(page, successTask())
    const chip = header(page).locator('[data-state]')
    const id = header(page).getByText(task.task_id, { exact: true })
    await expect(chip).toHaveAttribute('data-state', 'SUCCESS')
    await expect(id).toBeVisible()
    const chipBox = await chip.boundingBox()
    const idBox = await id.boundingBox()
    expect(Math.abs((chipBox?.y ?? 0) - (idBox?.y ?? 99))).toBeLessThan(12)
  })

  test('a failed task has the loud chip', async ({ page }) => {
    await openTask(page, createFailedTask('app.bad', 'ValueError: boom', 'tb'))
    await expect(header(page).locator('[data-state="FAILURE"]')).toHaveAttribute(
      'data-variant',
      'loud',
    )
  })

  test('copy ID writes the task ID to the clipboard', async ({ page, context }) => {
    await context.grantPermissions(['clipboard-read', 'clipboard-write'])
    const task = await openTask(page, successTask())
    await header(page).getByRole('button', { name: 'Copy task ID' }).click()
    await expect(header(page).getByRole('status')).toHaveText('Copied')
    expect(await page.evaluate(() => navigator.clipboard.readText())).toBe(task.task_id)
  })

  test('links go to the flow, the worker and every run of the task', async ({ page }) => {
    const task = successTask({ hostname: 'celery@worker-1' })
    for (const event of task.events) event.root_id = 'root-abc'
    await openTask(page, task)

    const flow = header(page).getByRole('link', { name: 'Open flow' })
    await expect(flow).toHaveAttribute('href', '/flows/root-abc')

    const worker = header(page).getByRole('link', { name: /^Worker/ })
    await expect(worker).toContainText('worker-1')
    const workerUrl = new URL((await worker.getAttribute('href')) ?? '', 'http://x')
    expect(workerUrl.pathname).toBe('/executions')
    expect([...workerUrl.searchParams]).toEqual([['hostname', 'celery@worker-1']])

    const runs = header(page).getByRole('link', { name: 'All runs of this task' })
    const runsUrl = new URL((await runs.getAttribute('href')) ?? '', 'http://x')
    expect(runsUrl.pathname).toBe('/executions')
    expect([...runsUrl.searchParams]).toEqual([['name_exact', task.name]])
  })

  test('the flow link falls back to the task itself when no event names a root', async ({
    page,
  }) => {
    const task = successTask()
    for (const event of task.events) event.root_id = null
    await openTask(page, task)
    await expect(header(page).getByRole('link', { name: 'Open flow' })).toHaveAttribute(
      'href',
      `/flows/${task.task_id}`,
    )
  })

  test('the worker link is omitted when no worker is known', async ({ page }) => {
    await openTask(page, successTask({ hostname: null }))
    await expect(header(page).getByRole('link', { name: 'Open flow' })).toBeVisible()
    await expect(header(page).getByRole('link', { name: /^Worker/ })).toHaveCount(0)
    await expect(header(page).getByText('–')).toHaveCount(0)
  })

  test('at 400px a long name keeps its last segment whole and the page does not scroll', async ({
    page,
  }) => {
    await page.setViewportSize({ width: 400, height: 800 })
    const long = 'billing.invoices.tasks.reminders.overdue.send_overdue_invoice_reminder'
    const task = await openTask(page, successTask({ name: long, hostname: 'celery@worker-1' }))
    const title = page.getByRole('heading', { level: 1 })
    await expect(title).toHaveText('send_overdue_invoice_reminder')
    const box = await title.boundingBox()
    expect((box?.x ?? 0) + (box?.width ?? 0)).toBeLessThanOrEqual(400)
    await expect(header(page).getByText(task.task_id, { exact: true })).toBeVisible()
    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
    )
    expect(overflow).toBe(0)
  })
})
