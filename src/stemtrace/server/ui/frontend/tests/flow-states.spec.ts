import { expect, type Page, test } from '@playwright/test'

import { reallocationFlow, serveFlow } from './fixtures/flow-graphs'
import { setupMockApi } from './fixtures/mock-api'

/** The Flow page before its graph arrives, and when the request fails. Mock mode only. */

test.skip(process.env.E2E_MODE === 'real', 'Mock-only test')

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

test.describe('Flow page: loading', () => {
  test('shows a skeleton of the page, not a spinner, until the flow arrives', async ({ page }) => {
    await page.routeWebSocket(/\/ws$/, () => {})
    await setupMockApi(page, { useDefaults: false })
    const flow = reallocationFlow()
    let release: () => void = () => {}
    const gate = new Promise<void>((resolve) => {
      release = resolve
    })
    await serveFlow(page, flow)
    await page.route(/\/api\/graphs\/[^/?]+(\?.*)?$/, async (route) => {
      await gate
      await route.fulfill({ json: { root_id: flow.root_id, nodes: flow.nodes } })
    })
    await page.goto(`/flows/${encodeURIComponent(flow.root_id)}`)

    const skeleton = page.getByTestId('flow-skeleton')
    await expect(skeleton).toBeVisible()
    await expect(page.locator('.animate-spin')).toHaveCount(0)
    await expect(skeleton.locator('.bg-surface').first()).toHaveCSS(
      'background-color',
      await token(page, 'surface'),
    )

    release()
    await expect(page.getByRole('region', { name: 'Timeline' })).toBeVisible()
    await expect(skeleton).toHaveCount(0)
  })
})

for (const theme of ['light', 'dark'] as const) {
  test.describe(`Flow page: error, ${theme}`, () => {
    test('says flow, in the app error colour, with a link in ink and no blue', async ({ page }) => {
      await page.addInitScript((t) => localStorage.setItem('stemtrace-theme', t), theme)
      await page.routeWebSocket(/\/ws$/, () => {})
      await setupMockApi(page, { useDefaults: false })
      await page.goto('/flows/nope')

      // The query retries three times before it gives up
      const alert = page.getByRole('alert')
      await expect(alert).toBeVisible({ timeout: 15_000 })
      await expect(alert).toContainText("Couldn't load the flow:")
      await expect(alert).not.toContainText(/graph/i)
      await expect(alert).toHaveCSS('background-color', await token(page, 'surface'))
      await expect(alert.getByTestId('notice-message')).toHaveCSS(
        'color',
        await token(page, 'failure-t'),
      )
      const back = alert.getByRole('link', { name: 'Back to flows' })
      await expect(back).toHaveAttribute('href', '/flows')
      await expect(back).toHaveCSS('color', await token(page, 'ink'))
    })
  })
}
