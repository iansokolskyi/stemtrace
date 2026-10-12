/**
 * Short display names for dotted task names.
 *
 * `app.billing.send` and `app.email.send` both end in `send`, so a page that
 * shows only the last segment draws two different tasks the same. Each name
 * gets instead the shortest dotted suffix that no other name on the page has:
 * `billing.send` and `email.send`, while `app.add` stays `add` when nothing
 * else ends in `add`. A name's full name is always unique, so the search ends.
 */

/** The last `depth` dotted segments of `name` (all of it if it has fewer). */
function suffixOf(name: string, depth: number): string {
  const parts = name.split('.')
  return parts.slice(Math.max(0, parts.length - depth)).join('.')
}

/**
 * Maps each distinct name in `names` to its shortest suffix that is unique
 * among them. Pass every name the page shows together, so two tasks on the
 * same screen never get the same label.
 */
export function shortestUniqueSuffixes(names: Iterable<string>): Map<string, string> {
  const distinct = [...new Set(names)]
  const labels = new Map<string, string>()
  let pending = distinct
  for (let depth = 1; pending.length > 0; depth++) {
    // Count over all names, not just the pending ones: a settled name's
    // suffix is as good a clash as any
    const counts = new Map<string, number>()
    for (const name of distinct) {
      const suffix = suffixOf(name, depth)
      counts.set(suffix, (counts.get(suffix) ?? 0) + 1)
    }
    pending = pending.filter((name) => {
      const suffix = suffixOf(name, depth)
      if (counts.get(suffix) !== 1) return true
      labels.set(name, suffix)
      return false
    })
  }
  return labels
}
