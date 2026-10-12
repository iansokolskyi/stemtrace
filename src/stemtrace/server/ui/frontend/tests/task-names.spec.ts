import { expect, type Page, test } from '@playwright/test'

import { setupMockApi } from './fixtures/mock-api'
import { createRegistryEntry, createSuccessTask, type MockTask } from './fixtures/mock-data'

/**
 * Tests that task names sharing their last segment stay told apart.
 *
 * The pages show a short name instead of the dotted one: `app.billing.send` and
 * `app.email.send` must not both read `send`. Each gets the shortest suffix no
 * other name on the page has (`billing.send`, `email.send`); a name nothing
 * else ends like stays one segment.
 */

test.describe('Task names - colliding short names', () => {
  test.skip(process.env.E2E_MODE === 'real', 'Mock-only test')

  /** A workflow whose root started two tasks that both end in `send`. */
  function sendWorkflow(): { root: MockTask; billing: MockTask; email: MockTask } {
    const root = createSuccessTask('app.flow.start')
    const billing = createSuccessTask('app.billing.send')
    const email = createSuccessTask('app.email.send')
    for (const child of [billing, email]) {
      child.root_id = root.task_id
      child.parent_id = root.task_id
      root.children.push(child.task_id)
    }
    return { root, billing, email }
  }

  /** Both colliding names are told apart, and no label is the bare `send`. */
  async function expectSendLabelsTold(page: Page) {
    await expect(page.getByText('billing.send', { exact: true })).toBeVisible()
    await expect(page.getByText('email.send', { exact: true })).toBeVisible()
    await expect(page.getByText('send', { exact: true })).toHaveCount(0)
  }

  test('graph nodes show the shortest name that is unique in the graph', async ({ page }) => {
    const mockApi = await setupMockApi(page, { useDefaults: false })
    const { root, billing, email } = sendWorkflow()
    mockApi.addTasks([root, billing, email])

    await page.goto(`/graph/${root.task_id}`)
    await page.locator('.react-flow__node').first().waitFor({ state: 'attached' })

    const billingNode = page.locator(`.react-flow__node[data-id="${billing.task_id}"]`)
    await expect(billingNode).toContainText('billing.send')
    await expect(billingNode.getByTitle('app.billing.send')).toHaveCount(1)
    await expect(page.locator(`.react-flow__node[data-id="${email.task_id}"]`)).toContainText(
      'email.send',
    )
    // Nothing else ends in `start`: one segment is enough
    await expect(page.locator(`.react-flow__node[data-id="${root.task_id}"]`)).toContainText(
      'start',
    )
    await expectSendLabelsTold(page)
  })

  test('timeline rows show the shortest unique name', async ({ page }) => {
    const mockApi = await setupMockApi(page, { useDefaults: false })
    const { root, billing, email } = sendWorkflow()
    mockApi.addTasks([root, billing, email])

    await page.goto('/?view=timeline')

    await expectSendLabelsTold(page)
    await expect(page.getByText('start', { exact: true })).toBeVisible()
  })

  test('child tasks list the shortest unique name', async ({ page }) => {
    const mockApi = await setupMockApi(page, { useDefaults: false })
    const { root, billing, email } = sendWorkflow()
    mockApi.addTasks([root, billing, email])

    await page.goto(`/tasks/${root.task_id}`)

    await expect(page.getByRole('heading', { name: 'Child Tasks' })).toBeVisible()
    await expectSendLabelsTold(page)
  })

  test('registry cards show the shortest unique name', async ({ page }) => {
    const mockApi = await setupMockApi(page, { useDefaults: false })
    mockApi.setRegistry([
      createRegistryEntry('app.billing.send'),
      createRegistryEntry('app.email.send'),
      createRegistryEntry('app.add'),
    ])

    await page.goto('/registry')

    const billing = page.getByRole('heading', { name: 'billing.send', level: 3 })
    await expect(billing).toBeVisible()
    // The full name stays available on hover
    await expect(billing).toHaveAttribute('title', 'app.billing.send')
    await expect(page.getByRole('heading', { name: 'email.send', level: 3 })).toBeVisible()
    await expect(page.getByRole('heading', { name: 'add', level: 3, exact: true })).toBeVisible()
    await expect(page.getByRole('heading', { name: 'send', level: 3, exact: true })).toHaveCount(0)
  })

  test('names that only differ further up are told apart by as many segments as it takes', async ({
    page,
  }) => {
    const mockApi = await setupMockApi(page, { useDefaults: false })
    mockApi.setRegistry([
      createRegistryEntry('app.a.b.send'),
      createRegistryEntry('app.c.b.send'),
      createRegistryEntry('app.send'),
    ])

    await page.goto('/registry')

    // `b.send` would still be shared by the first two; `app.send` is unique at two segments
    for (const label of ['a.b.send', 'c.b.send', 'app.send']) {
      await expect(page.getByRole('heading', { name: label, level: 3, exact: true })).toBeVisible()
    }
    await expect(page.getByRole('heading', { level: 3 })).toHaveCount(3)
  })

  test('worker task chips show the shortest unique name', async ({ page }) => {
    const mockApi = await setupMockApi(page, { useDefaults: false })
    mockApi.setWorkers([
      {
        hostname: 'worker-1',
        pid: 1,
        registered_tasks: ['app.billing.send', 'app.add'],
        status: 'online',
        registered_at: new Date().toISOString(),
        last_seen: new Date().toISOString(),
      },
      {
        hostname: 'worker-2',
        pid: 2,
        registered_tasks: ['app.email.send'],
        status: 'online',
        registered_at: new Date().toISOString(),
        last_seen: new Date().toISOString(),
      },
    ])

    await page.goto('/workers')

    // One chip per worker's task, across all workers on the page
    await expectSendLabelsTold(page)
    await expect(page.getByText('add', { exact: true })).toBeVisible()
  })
})
