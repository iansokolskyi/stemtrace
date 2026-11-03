/** Characters after which the next one starts a word. */
const WORD_START_AFTER = '.-_/ :@'

/**
 * Subsequence match of `query` in `text`, case-insensitive; -1 when it is not one. A character
 * scores 1, 3 more when it follows the previous match directly, 3 more at a word start; the
 * length of `text` costs 0.02 per character, so of two equal matches the shorter ranks first.
 * Ported from the prototype's `fuzzy`.
 */
export function fuzzy(query: string, text: string): number {
  const q = query.toLowerCase()
  const s = text.toLowerCase()
  let matched = 0
  let score = 0
  let last = -2
  for (let j = 0; j < s.length && matched < q.length; j++) {
    if (s[j] !== q[matched]) continue
    score += j === last + 1 ? 3 : 1
    if (j === 0 || WORD_START_AFTER.includes(s[j - 1])) score += 3
    last = j
    matched++
  }
  return matched === q.length ? score - s.length * 0.02 : -1
}
