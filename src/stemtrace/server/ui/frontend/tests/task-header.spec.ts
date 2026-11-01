import { expect, type Locator, type Page, test } from '@playwright/test'

import { setupMockApi } from './fixtures/mock-api'
import {
  createChain,
  createFailedTask,
  createGroup,
  createWorkflowWithGroup,
  type MockTask,
} from './fixtures/mock-data'
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
    const task = successTask({ hostname: 'celery@worker-1', flow_root_id: 'root-abc' })
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

  test('the worker link is omitted when no worker is known', async ({ page }) => {
    await openTask(page, successTask({ hostname: null }))
    await expect(header(page).getByRole('link', { name: 'All runs of this task' })).toBeVisible()
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

// The flow root is the API's `flow_root_id` (where the graph places the task). Celery sets every
// event's `root_id` to the task's own ID for a group member or any task sent without a parent, so
// these tests give the events that wrong answer and expect the link to ignore it.
test.describe('TaskHeader flow link', () => {
  /** Serves `tasks` and returns the "Open flow" href of each task page, by task ID. */
  async function flowHrefs(
    page: Page,
    tasks: MockTask[],
    ids: string[],
  ): Promise<Record<string, string | null>> {
    const api = await setupMockApi(page, { useDefaults: false })
    api.addTasks(tasks)
    const hrefs: Record<string, string | null> = {}
    for (const id of ids) {
      await page.goto(`/tasks/${id}`)
      await expect(page.getByRole('heading', { level: 1 })).toBeVisible()
      const link = header(page).getByRole('link', { name: 'Open flow' })
      // The router writes `group:` IDs with the colon encoded
      hrefs[id] = (await link.count())
        ? decodeURIComponent((await link.getAttribute('href')) ?? '')
        : null
    }
    return hrefs
  }

  /** Gives every event the Celery default: this task is its own root. */
  const selfRooted = (tasks: MockTask[]): void => {
    for (const task of tasks) for (const event of task.events) event.root_id = task.task_id
  }

  test('a top-level group page and its member pages open the group node', async ({ page }) => {
    const [node, ...members] = createGroup(2)
    selfRooted(members)
    const hrefs = await flowHrefs(
      page,
      [node, ...members],
      [node.task_id, ...members.map((m) => m.task_id)],
    )
    for (const id of Object.keys(hrefs)) expect(hrefs[id]).toBe(`/flows/${node.task_id}`)
  })

  test('every step of a chain opens the first task', async ({ page }) => {
    const chain = createChain('app.chain', 3)
    selfRooted(chain)
    const hrefs = await flowHrefs(
      page,
      chain,
      chain.map((t) => t.task_id),
    )
    for (const task of chain) expect(hrefs[task.task_id]).toBe(`/flows/${chain[0].task_id}`)
  })

  test('a chord header and its callback open the chord node', async ({ page }) => {
    const [node, ...rest] = createGroup(2, { withCallback: true })
    selfRooted(rest)
    const header0 = rest[0]
    const callback = rest[rest.length - 1]
    const hrefs = await flowHrefs(
      page,
      [node, ...rest],
      [node.task_id, header0.task_id, callback.task_id],
    )
    for (const id of Object.keys(hrefs)) expect(hrefs[id]).toBe(`/flows/${node.task_id}`)
  })

  test('a group started inside a task opens that task, from every node', async ({ page }) => {
    const [parent, node, ...members] = createWorkflowWithGroup(2)
    selfRooted(members)
    const hrefs = await flowHrefs(
      page,
      [parent, node, ...members],
      [parent.task_id, node.task_id, ...members.map((m) => m.task_id)],
    )
    for (const id of Object.keys(hrefs)) expect(hrefs[id]).toBe(`/flows/${parent.task_id}`)
  })

  test('the link uses what the API says even when the events name another root', async ({
    page,
  }) => {
    const task = successTask({ flow_root_id: 'root-from-api' })
    for (const event of task.events) event.root_id = 'root-from-events'
    await openTask(page, task)
    await expect(header(page).getByRole('link', { name: 'Open flow' })).toHaveAttribute(
      'href',
      '/flows/root-from-api',
    )
  })

  test('with no flow root there is no link, whatever the events say', async ({ page }) => {
    const task = successTask({ flow_root_id: null })
    for (const event of task.events) event.root_id = task.task_id
    await openTask(page, task)
    await expect(header(page).getByRole('heading', { level: 1 })).toBeVisible()
    await expect(header(page).getByRole('link', { name: 'Open flow' })).toHaveCount(0)
  })
})
