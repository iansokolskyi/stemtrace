import { expect, test } from '@playwright/test'

import { fuzzy } from '../src/components/palette/fuzzy'

/** The palette's fuzzy match (ported from the prototype's `fuzzy`), no browser. */

const rank = (query: string, names: string[]): string[] =>
  names
    .map((name) => ({ name, score: fuzzy(query, name) }))
    .filter((x) => x.score >= 0)
    .sort((a, b) => b.score - a.score)
    .map((x) => x.name)

test('a query that is not a subsequence scores -1', () => {
  expect(fuzzy('xyz', 'tasks.add')).toBe(-1)
  expect(fuzzy('adda', 'tasks.add')).toBe(-1)
})

test('an empty query matches with the length penalty only', () => {
  expect(fuzzy('', 'abcd')).toBeCloseTo(-0.08, 10)
})

test('matching ignores case on both sides', () => {
  expect(fuzzy('ADD', 'tasks.add')).toBe(fuzzy('add', 'TASKS.ADD'))
})

test('the score is exact: consecutive +3 else +1, word start +3, minus 0.02 per character', () => {
  // "add" in "tasks.add": a at 6 (word start after ".": 1+3), d at 7 (consecutive: 3), d at 8 (3)
  expect(fuzzy('add', 'tasks.add')).toBeCloseTo(4 + 3 + 3 - 9 * 0.02, 10)
  // "ta" in "tasks.add": t at 0 (1+3), a at 1 (consecutive: 3)
  expect(fuzzy('ta', 'tasks.add')).toBeCloseTo(4 + 3 - 9 * 0.02, 10)
  // "tk" in "tasks": t at 0 (1+3), k at 3 (not consecutive, not a word start: 1)
  expect(fuzzy('tk', 'tasks')).toBeCloseTo(4 + 1 - 5 * 0.02, 10)
})

test('every word separator starts a word', () => {
  for (const sep of ['.', '-', '_', '/', ' ', ':', '@']) {
    expect(fuzzy('b', `a${sep}b`), sep).toBeCloseTo(4 - 3 * 0.02, 10)
  }
  expect(fuzzy('b', 'axb')).toBeCloseTo(1 - 3 * 0.02, 10)
})

test('consecutive characters outrank scattered ones', () => {
  expect(rank('send', ['sxexnxd', 'resend_all', 'send'])).toEqual(['send', 'resend_all', 'sxexnxd'])
})

test('a match at a word start outranks one inside a word', () => {
  expect(rank('rec', ['shop.mail.direct', 'shop.orders.reconcile'])).toEqual([
    'shop.orders.reconcile',
    'shop.mail.direct',
  ])
})

test('of two equal matches the shorter name ranks first', () => {
  expect(rank('add', ['tasks.add_numbers', 'tasks.add'])).toEqual(['tasks.add', 'tasks.add_numbers'])
})
