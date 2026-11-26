/** What a triage panel entry shows. `anomaly` is parsed here; its body belongs to the Anomalies work. */
export const PANEL_KINDS = ['runs', 'exception', 'anomaly', 'worker', 'group', 'task'] as const

export type PanelKind = (typeof PANEL_KINDS)[number]

/** The state chips of a runs entry, in the order the panel lists them. */
export const STATE_CHIPS = ['FAILURE', 'RETRY', 'STARTED', 'PENDING', 'SUCCESS'] as const

/** The most "Show more" clicks an entry keeps in the URL; a larger value is the default. */
export const MAX_MORE = 50

/**
 * One level of the panel's stack: what it shows (`kind`) and which one (`id`, any text), and the
 * view the entry was left in: the state chips picked and the "Show more" clicks. An entry has
 * `states` and `more` only when they are not the default (no chips, 0 clicks).
 */
export interface PanelEntry {
  kind: PanelKind
  id: string
  states?: string[]
  more?: number
}

const isKind = (text: string): text is PanelKind =>
  (PANEL_KINDS as readonly string[]).includes(text)

/** The chips named in a comma-separated list: known ones only, upper-cased, each once, in order. */
function parseChips(value: string): string[] {
  const kept: string[] = []
  for (const part of value.split(',')) {
    const state = part.toUpperCase()
    if ((STATE_CHIPS as readonly string[]).includes(state) && !kept.includes(state)) {
      kept.push(state)
    }
  }
  return kept
}

/** The view keys (`s=FAILURE,RETRY`, `m=2`) after an entry; the first of a key wins. */
function parseView(keys: readonly string[]): Pick<PanelEntry, 'states' | 'more'> {
  const view: Pick<PanelEntry, 'states' | 'more'> = {}
  const seen = new Set<string>()
  for (const key of keys) {
    const cut = key.indexOf('=')
    if (cut < 0) continue
    const name = key.slice(0, cut)
    const value = key.slice(cut + 1)
    if (seen.has(name)) continue
    seen.add(name)
    if (name === 's') {
      const states = parseChips(value)
      if (states.length) view.states = states
    } else if (name === 'm' && /^[1-9]\d{0,2}$/.test(value) && Number(value) <= MAX_MORE) {
      view.more = Number(value)
    }
  }
  return view
}

/**
 * The stack from the `panel` URL value: `kind:id` entries joined by `/`, each id passed through
 * `encodeURIComponent`, then the entry's view as `@s=<chips>` and `@m=<count>`. An `@` in an id
 * is always encoded, so a raw one only starts a view key. An entry with an unknown kind, no `:`,
 * an empty id or an id that does not decode is dropped, and an invalid view value is the
 * default, so a hand-edited or old link opens what it can.
 */
export function parsePanel(value: string | undefined): PanelEntry[] {
  if (!value) return []
  const entries: PanelEntry[] = []
  for (const part of value.split('/')) {
    const [head, ...viewKeys] = part.split('@')
    // Only the first `:` separates: an encoded id holds none, but a stray one is kept in the id
    const cut = head.indexOf(':')
    if (cut < 0) continue
    const kind = head.slice(0, cut)
    let id: string
    try {
      id = decodeURIComponent(head.slice(cut + 1))
    } catch {
      continue
    }
    if (isKind(kind) && id) entries.push({ kind, id, ...parseView(viewKeys) })
  }
  return entries
}

function formatEntry({ kind, id, states, more }: PanelEntry): string {
  let text = `${kind}:${encodeURIComponent(id)}`
  const chips = parseChips((states ?? []).join(','))
  if (chips.length) text += `@s=${chips.join(',')}`
  if (more && Number.isInteger(more) && more >= 1 && more <= MAX_MORE) text += `@m=${more}`
  return text
}

/** The `panel` URL value for a stack; undefined when it is empty (no param). */
export function formatPanel(entries: readonly PanelEntry[]): string | undefined {
  const parts = entries.filter((e) => e.id).map(formatEntry)
  return parts.length ? parts.join('/') : undefined
}
