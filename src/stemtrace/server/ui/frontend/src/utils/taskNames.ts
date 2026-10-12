/**
 * Short display names for dotted task names.
 *
 * `app.billing.send` and `app.email.send` both end in `send`, so a page that
 * shows only the last segment draws two different tasks the same. Each name
 * gets instead the shortest dotted suffix that no other name on the page has:
 * `billing.send` and `email.send`, while `app.add` stays `add` when nothing
 * else ends in `add`. A name's full name is always unique, so the search ends.
 */

/**
 * Maps each distinct name in `names` to its shortest suffix that is unique
 * among them. Pass every name the page shows together, so two tasks on the
 * same screen never get the same label.
 */
export function shortestUniqueSuffixes(names: Iterable<string>): Map<string, string> {
  const distinct = [...new Set(names)]
  const segments = distinct.map((name) => name.split('.'))
  const labels = new Map<string, string>()
  let pending = distinct.map((_, index) => index)
  for (let depth = 1; pending.length > 0; depth++) {
    const suffixes = pending.map((index) => segments[index].slice(-depth).join('.'))
    // Only the names still pending need counting. A name settled at a lower
    // depth has a suffix no other name shares at that depth, and so none at a
    // greater one either (a name with fewer segments than `depth` is its own
    // suffix, which a longer name never equals).
    const counts = new Map<string, number>()
    for (const suffix of suffixes) counts.set(suffix, (counts.get(suffix) ?? 0) + 1)
    pending = pending.filter((index, position) => {
      const suffix = suffixes[position]
      if (counts.get(suffix) !== 1) return true
      labels.set(distinct[index], suffix)
      return false
    })
  }
  return labels
}
