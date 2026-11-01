import { expect, type Locator } from '@playwright/test'

/** The text of each column header of a table, in order. */
export async function columnHeaders(table: Locator): Promise<(string | null)[]> {
  return table
    .getByRole('columnheader')
    .evaluateAll((cells) => cells.map((cell) => cell.textContent))
}

/** The status icon block is filled with the colour of the theme token, e.g. `--failure`. */
export async function expectFilledWithToken(block: Locator, token: string): Promise<void> {
  const got = await block.evaluate((el, name) => {
    const probe = document.createElement('div')
    probe.style.backgroundColor = `var(${name})`
    document.body.append(probe)
    const want = getComputedStyle(probe).backgroundColor
    probe.remove()
    return { actual: getComputedStyle(el).backgroundColor, want }
  }, token)
  expect(got.actual).toBe(got.want)
}
