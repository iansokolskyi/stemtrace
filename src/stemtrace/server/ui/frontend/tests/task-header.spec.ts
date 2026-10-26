import { expect, type Locator, type Page, test } from '@playwright/test'

import { createFailedTask, createGroup } from './fixtures/mock-data'
import { openTask, successTask, tokenColour } from './fixtures/task-page'

/** The top of the task page: title, state, ID, links. Mock mode: it controls the optional fields. */

test.skip(process.env.E2E_MODE === 'real', 'Mock-only test')

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

// A GROUP or CHORD node is synthetic: it has no events and is never a flow's root itself, so its
// flow is the one its members belong to.
test.describe('TaskHeader on a group page', () => {
  for (const withCallback of [false, true]) {
    const kind = withCallback ? 'chord' : 'group'

    test(`a ${kind} opens the flow its members belong to`, async ({ page }) => {
      const [node, ...members] = createGroup(2, { withCallback })
      for (const member of members) for (const event of member.events) event.root_id = 'root-xyz'
      await openTask(page, node, ...members)
      await expect(header(page).getByRole('link', { name: 'Open flow' })).toHaveAttribute(
        'href',
        '/flows/root-xyz',
      )
    })

    test(`a ${kind} skips members that name no root and uses the first that does`, async ({
      page,
    }) => {
      const [node, ...members] = createGroup(3, { withCallback })
      const [first, ...rest] = members
      for (const event of first.events) event.root_id = null
      for (const member of rest) for (const event of member.events) event.root_id = 'root-later'
      await openTask(page, node, ...members)
      await expect(header(page).getByRole('link', { name: 'Open flow' })).toHaveAttribute(
        'href',
        '/flows/root-later',
      )
    })

    test(`a ${kind} whose members name no root has no flow link`, async ({ page }) => {
      const [node, ...members] = createGroup(2, { withCallback })
      for (const member of members) for (const event of member.events) event.root_id = null
      await openTask(page, node, ...members)
      await expect(header(page).getByRole('heading', { level: 1 })).toBeVisible()
      await expect(header(page).getByRole('link', { name: 'Open flow' })).toHaveCount(0)
    })
  }

  test('a group with no members has no flow link', async ({ page }) => {
    const [node] = createGroup(0)
    await openTask(page, node)
    await expect(header(page).getByRole('link', { name: 'Open flow' })).toHaveCount(0)
  })
})
