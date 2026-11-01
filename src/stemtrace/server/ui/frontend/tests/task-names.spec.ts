import { expect, type Locator, type Page, test } from '@playwright/test'
import { shortestUniqueSuffixes } from '../src/utils/taskNames'
import { flowNode, flowNodes } from './fixtures/flow'
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
  async function expectSendLabelsTold(page: Page, within: Page | Locator = page) {
    await expect(within.getByText('billing.send', { exact: true })).toBeVisible()
    await expect(within.getByText('email.send', { exact: true })).toBeVisible()
    await expect(within.getByText('send', { exact: true })).toHaveCount(0)
  }

  test('graph nodes show the shortest name that is unique in the graph', async ({ page }) => {
    const mockApi = await setupMockApi(page, { useDefaults: false })
    const { root, billing, email } = sendWorkflow()
    mockApi.addTasks([root, billing, email])

    await page.goto(`/flows/${root.task_id}`)
    await flowNodes(page).first().waitFor({ state: 'attached' })

    const billingNode = flowNode(page, billing.task_id)
    await expect(billingNode).toContainText('billing.send')
    await expect(billingNode.getByTitle('app.billing.send')).toHaveCount(1)
    await expect(flowNode(page, email.task_id)).toContainText('email.send')
    // Nothing else ends in `start`: one segment is enough
    await expect(flowNode(page, root.task_id)).toContainText('start')
    await expectSendLabelsTold(page, page.getByTestId('flow-canvas'))
    // The Timeline under the diagram tells them apart the same way
    await expectSendLabelsTold(page, page.getByRole('region', { name: 'Timeline' }))
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

  test('graph nodes and timeline links carry the full task name for screen readers', async ({
    page,
  }) => {
    const mockApi = await setupMockApi(page, { useDefaults: false })
    const { root, billing, email } = sendWorkflow()
    mockApi.addTasks([root, billing, email])

    await page.goto(`/flows/${root.task_id}`)
    await flowNodes(page).first().waitFor({ state: 'attached' })
    const diagram = page.getByTestId('flow-canvas')
    const timeline = page.getByRole('region', { name: 'Timeline' })
    await expect(diagram.getByRole('button', { name: /app\.billing\.send/ })).toHaveCount(1)
    await expect(diagram.getByRole('button', { name: /app\.email\.send/ })).toHaveCount(1)
    await expect(timeline.getByRole('button', { name: /^app\.billing\.send,/ })).toHaveCount(1)
    await expect(timeline.getByRole('button', { name: /^app\.email\.send,/ })).toHaveCount(1)

    await page.goto('/?view=timeline')
    await expect(page.getByRole('link', { name: 'app.billing.send' })).toHaveCount(1)
    await expect(page.getByRole('link', { name: 'app.email.send' })).toHaveCount(1)
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
})

test.describe('shortestUniqueSuffixes', () => {
  // A pure function: no page needed
  const SEGMENTS = ['a', 'b', 'c', 'send', 'add', 'app', 'x']

  /** A reproducible generator of lists of dotted names with many shared segments. */
  function randomNameLists(count: number): string[][] {
    let seed = 12345
    const below = (limit: number) => {
      seed = (seed * 1103515245 + 12345) & 0x7fffffff
      return seed % limit
    }
    return Array.from({ length: count }, () =>
      Array.from({ length: 1 + below(12) }, () =>
        Array.from({ length: 1 + below(5) }, () => SEGMENTS[below(SEGMENTS.length)]).join('.'),
      ),
    )
  }

  test('labels are unique suffixes, and no shorter suffix would have been unique', () => {
    // Plain checks collecting the first broken case: `expect` per name is too slow for 20,000 lists
    const problems: string[] = []
    const isSuffix = (name: string, suffix: string) =>
      name === suffix || name.endsWith(`.${suffix}`)
    for (const names of randomNameLists(20_000)) {
      const labels = shortestUniqueSuffixes(names)
      const distinct = [...new Set(names)]
      if (new Set(labels.values()).size !== distinct.length) problems.push(`not unique: ${names}`)
      for (const name of distinct) {
        const label = labels.get(name)
        if (label === undefined || !isSuffix(name, label)) {
          problems.push(`${label} is not a suffix of ${name}`)
          continue
        }
        const depth = label.split('.').length
        if (depth > 1) {
          // One segment fewer is shared by another name, so this was the shortest
          const shorter = name
            .split('.')
            .slice(-(depth - 1))
            .join('.')
          if (distinct.filter((other) => isSuffix(other, shorter)).length < 2) {
            problems.push(`${shorter} of ${name} would have been unique in ${names}`)
          }
        }
      }
      if (problems.length > 0) break
    }
    expect(problems).toEqual([])
  })
})
