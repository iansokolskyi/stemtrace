import { readFileSync } from 'node:fs'

import { expect, test } from '@playwright/test'

import { sourceProblems } from '../scripts/type-scale-rules.mjs'

/**
 * The type-scale guard (`npm run check`) must flag every way of setting an off-scale font size and
 * pass the six sizes. Snippets live in `fixtures/type-scale-mutants.json`; no browser is used.
 */

const fixtures: { fail: string[]; pass: string[] } = JSON.parse(
  readFileSync(new URL('./fixtures/type-scale-mutants.json', import.meta.url), 'utf8'),
)

for (const snippet of fixtures.fail) {
  test(`guard flags ${snippet}`, () => {
    expect(sourceProblems(snippet).length).toBeGreaterThan(0)
  })
}

for (const snippet of fixtures.pass) {
  test(`guard passes ${snippet}`, () => {
    expect(sourceProblems(snippet)).toEqual([])
  })
}
