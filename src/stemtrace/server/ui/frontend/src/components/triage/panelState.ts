/** What a triage panel entry shows. `anomaly` is parsed here; its body belongs to the Anomalies work. */
export const PANEL_KINDS = ['runs', 'exception', 'anomaly', 'worker', 'group', 'task'] as const

export type PanelKind = (typeof PANEL_KINDS)[number]

/** One level of the panel's stack: what it shows (`kind`) and which one (`id`, any text). */
export interface PanelEntry {
  kind: PanelKind
  id: string
}

const isKind = (text: string): text is PanelKind =>
  (PANEL_KINDS as readonly string[]).includes(text)

/**
 * The stack from the `panel` URL value: `kind:id` entries joined by `/`, each id passed through
 * `encodeURIComponent`. An entry with an unknown kind, no `:`, an empty id or an id that does not
 * decode is dropped, so a hand-edited or old link opens what it can.
 */
export function parsePanel(value: string | undefined): PanelEntry[] {
  if (!value) return []
  const entries: PanelEntry[] = []
  for (const part of value.split('/')) {
    // Only the first `:` separates: an encoded id holds none, but a stray one is kept in the id
    const cut = part.indexOf(':')
    if (cut < 0) continue
    const kind = part.slice(0, cut)
    let id: string
    try {
      id = decodeURIComponent(part.slice(cut + 1))
    } catch {
      continue
    }
    if (isKind(kind) && id) entries.push({ kind, id })
  }
  return entries
}

/** The `panel` URL value for a stack; undefined when it is empty (no param). */
export function formatPanel(entries: readonly PanelEntry[]): string | undefined {
  const parts = entries.filter((e) => e.id).map((e) => `${e.kind}:${encodeURIComponent(e.id)}`)
  return parts.length ? parts.join('/') : undefined
}
