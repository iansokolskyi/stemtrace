import { expect, type Locator, type Page, test } from '@playwright/test'

import { flowNode } from './fixtures/flow'
import { allCaptures, serveFlow } from './fixtures/flow-graphs'
import { setupMockApi } from './fixtures/mock-api'

/**
 * The Flows list titles a GROUP or CHORD root as the flow page's box does ("Chord of 12 fetch"),
 * from the `member_count` and `member_name` the list sends. A TASK root, and a server that sends
 * no member fields, keep the root's short name. Mock mode only.
 */

test.skip(process.env.E2E_MODE === 'real', 'Mock-only test')

const rows = (page: Page): Locator => page.getByTestId('flow-row')
const titleOf = (row: Locator): Locator => row.locator('b')

interface ListRoot {
  id: string
  name: string
  type: 'TASK' | 'GROUP' | 'CHORD'
  state?: string
  /** Omit for a server that sends no member fields */
  members?: { member_count: number | null; member_name: string | null }
}

/** The list item the server sends for a root. */
function listItem(root: ListRoot): Record<string, unknown> {
  const state = root.state ?? 'SUCCESS'
  return {
    task_id: root.id,
    name: root.name,
    state,
    node_type: root.type,
    group_id: null,
    chord_id: null,
    parent_id: null,
    reported_parent_id: null,
    children: [],
    duration_ms: null,
    first_seen: '2026-10-17T11:00:00Z',
    last_updated: '2026-10-17T11:00:00Z',
    flow: {
      task_count: 3,
      state_counts: { [state]: 3 },
      not_run_count: 0,
      state,
      ...root.members,
    },
  }
}

/** Answers GET /api/graphs with what `roots()` returns at the time of each request. */
async function serveList(page: Page, roots: () => ListRoot[]): Promise<void> {
  await page.route(/\/api\/graphs(\?|$)/, (route) => {
    const list = roots()
    return route.fulfill({
      json: { graphs: list.map(listItem), total: list.length, limit: 50, offset: 0 },
    })
  })
}

async function openList(page: Page, roots: ListRoot[]): Promise<void> {
  await setupMockApi(page, { useDefaults: false })
  await serveList(page, () => roots)
  await page.goto('/flows')
  await expect(rows(page)).toHaveCount(roots.length)
}

const container = (
  count: number | null,
  name: string | null,
  type: ListRoot['type'] = 'GROUP',
): ListRoot => ({
  id: `r-${type}-${count}`,
  name: 'app.root',
  type,
  members: { member_count: count, member_name: name },
})

test.describe('Flows list titles', () => {
  test('row 1: a group of three resize tasks reads "Group of 3 resize"', async ({ page }) => {
    await openList(page, [container(3, 'resize')])
    await expect(titleOf(rows(page).first())).toHaveText('Group of 3 resize')
  })

  test('row 2: a chord says Chord, with the member count and name', async ({ page }) => {
    await openList(page, [container(12, 'fetch', 'CHORD')])
    await expect(titleOf(rows(page).first())).toHaveText('Chord of 12 fetch')
  })

  test('row 3: a group of chains uses the name the server shares', async ({ page }) => {
    await openList(page, [container(2, 'b')])
    await expect(titleOf(rows(page).first())).toHaveText('Group of 2 b')
  })

  test('row 4: members with different names read "Group of 2 tasks"', async ({ page }) => {
    await openList(page, [container(2, null)])
    await expect(titleOf(rows(page).first())).toHaveText('Group of 2 tasks')
  })

  test('row 5: the count the server sends for a nested header is shown as sent', async ({
    page,
  }) => {
    // The inner container is no row, so the server counts 2 and not 3
    await openList(page, [container(2, 'add', 'CHORD')])
    await expect(titleOf(rows(page).first())).toHaveText('Chord of 2 add')
  })

  test('row 6: a flow still arriving grows its count on the next load', async ({ page }) => {
    let count = 2
    await setupMockApi(page, { useDefaults: false })
    await serveList(page, () => [{ ...container(count, 'fetch'), state: 'STARTED' }])
    await page.goto('/flows')
    await expect(titleOf(rows(page).first())).toHaveText('Group of 2 fetch')
    count = 5
    await page.reload()
    await expect(titleOf(rows(page).first())).toHaveText('Group of 5 fetch')
  })

  test('row 7: a container with no members yet reads "Group of 0 tasks"', async ({ page }) => {
    await openList(page, [container(0, null)])
    await expect(titleOf(rows(page).first())).toHaveText('Group of 0 tasks')
  })

  test('row 8: a TASK root keeps its short name, with or without member fields', async ({
    page,
  }) => {
    await openList(page, [
      { id: 'r-task', name: 'billing.invoices.close_month', type: 'TASK' },
      {
        id: 'r-task-fields',
        name: 'billing.invoices.close_month',
        type: 'TASK',
        members: { member_count: 3, member_name: 'x' },
      },
    ])
    await expect(titleOf(rows(page).nth(0))).toHaveText('close_month')
    await expect(titleOf(rows(page).nth(1))).toHaveText('close_month')
  })

  test('a server without member fields falls back to the root short name', async ({ page }) => {
    await openList(page, [
      { id: 'r-old-group', name: 'tools.fan_out', type: 'GROUP' },
      { id: 'r-old-chord', name: 'tools.merge_all', type: 'CHORD' },
    ])
    await expect(titleOf(rows(page).nth(0))).toHaveText('fan_out')
    await expect(titleOf(rows(page).nth(1))).toHaveText('merge_all')
  })

  test('a container root with a null count falls back to the root short name', async ({ page }) => {
    await openList(page, [
      { ...container(null, null), id: 'r-null', name: 'tools.fan_out' },
    ])
    await expect(titleOf(rows(page).first())).toHaveText('fan_out')
  })

  test('a name the server resolved from an alias shows as sent', async ({ page }) => {
    // shop.eu resolves to its last segment, eu, on the server (node_alias_from_arguments)
    await openList(page, [container(4, 'eu')])
    await expect(titleOf(rows(page).first())).toHaveText('Group of 4 eu')
  })

  test('a long member name is cut and the row stays inside 400 px', async ({ page }) => {
    await page.setViewportSize({ width: 400, height: 800 })
    await openList(page, [container(7, 'a_very_long_member_task_name_'.repeat(8), 'CHORD')])
    const row = rows(page).first()
    const title = titleOf(row)
    const [rowBox, titleBox, cut] = await Promise.all([
      row.boundingBox(),
      title.boundingBox(),
      title.evaluate((el) => el.scrollWidth > el.clientWidth),
    ])
    expect(cut).toBe(true)
    expect(titleBox?.width).toBeLessThanOrEqual(rowBox?.width ?? 0)
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(400)
  })
})

test.describe('List title equals the flow page title', () => {
  // Captured flows: a shared member name (chord, group) and mixed names (chord_failed_header),
  // a GROUP and a CHORD root
  for (const wanted of ['chord', 'group', 'chord_failed_header', 'chain_group_tsum_add@celery5.2']) {
    test(`capture ${wanted}: the row's title is the root box's title`, async ({ page }) => {
      const found = allCaptures()[wanted]
      expect(found, wanted).toBeTruthy()
      const graph = structuredClone(found)
      const root = graph.nodes[graph.root_id]
      expect(graph.member_fields?.member_count, wanted).toBeGreaterThan(0)
      await setupMockApi(page, { useDefaults: false })
      await serveFlow(page, graph)
      await serveList(page, () => [
        {
          id: graph.root_id,
          name: root.name,
          type: root.node_type,
          members: graph.member_fields,
        },
      ])
      await page.goto('/flows')
      const listTitle = (await titleOf(rows(page).first()).textContent()) ?? ''
      expect(listTitle).toMatch(/^(Group|Chord) of \d+ /)
      await rows(page).first().click()
      const box = flowNode(page, graph.root_id)
      await expect(box).toBeVisible()
      const label = (await box.getAttribute('aria-label')) ?? ''
      expect(label.startsWith(`${listTitle}, `), `${label} vs ${listTitle}`).toBe(true)
    })
  }
})
