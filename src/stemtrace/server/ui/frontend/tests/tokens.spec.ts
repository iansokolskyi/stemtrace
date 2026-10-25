/**
 * Signal box token checks (no browser): text tokens meet WCAG AA on every surface, in both
 * themes, and the two copies of the dark tokens (OS preference and the stored override)
 * never drift apart.
 */
import { readFileSync } from 'node:fs'
import { expect, test } from '@playwright/test'

type Mode = 'light' | 'dark'

const CSS = readFileSync(new URL('../src/styles/tokens.css', import.meta.url), 'utf8')

function block(css: string, selector: RegExp): string {
  const start = css.search(selector)
  if (start < 0) throw new Error(`no block matching ${selector}`)
  const open = css.indexOf('{', start)
  let depth = 0
  for (let i = open; i < css.length; i++) {
    if (css[i] === '{') depth++
    else if (css[i] === '}' && --depth === 0) return css.slice(open + 1, i)
  }
  throw new Error(`unclosed block for ${selector}`)
}

function declarations(body: string): Record<string, string> {
  const out: Record<string, string> = {}
  for (const m of body.matchAll(/--([\w-]+)\s*:\s*([^;]+);/g)) out[m[1]] = m[2].trim()
  return out
}

/** The token map for a theme; `var(--x)` references are resolved within that map. */
export function parseTokens(css: string, mode: Mode): Record<string, string> {
  const light = declarations(block(css, /^:root\s*\{/m))
  const raw =
    mode === 'light'
      ? light
      : { ...light, ...declarations(block(css, /:root\[data-theme="dark"\]\s*\{/)) }
  const resolve = (v: string, seen = 0): string => {
    const ref = /^var\(--([\w-]+)\)$/.exec(v)
    return ref && seen < 10 ? resolve(raw[ref[1]] ?? v, seen + 1) : v
  }
  return Object.fromEntries(Object.entries(raw).map(([k, v]) => [k, resolve(v)]))
}

function luminance(hex: string): number {
  const m = /^#([0-9a-f]{6})$/i.exec(hex)
  if (!m) throw new Error(`not a #rrggbb colour: ${hex}`)
  const [r, g, b] = [0, 2, 4].map((i) => {
    const c = Number.parseInt(m[1].slice(i, i + 2), 16) / 255
    return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4
  })
  return 0.2126 * r + 0.7152 * g + 0.0722 * b
}

/** WCAG 2.x contrast ratio between two #rrggbb colours. */
export function contrast(a: string, b: string): number {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x)
  return (hi + 0.05) / (lo + 0.05)
}

const STATES = ['success', 'failure', 'retry', 'running', 'pending', 'anomaly']

for (const mode of ['light', 'dark'] as const) {
  test(`text tokens reach 4.5:1 on every surface (${mode})`, () => {
    const t = parseTokens(CSS, mode)
    for (const fg of ['ink', 'muted', ...STATES.map((s) => `${s}-t`)]) {
      for (const bg of ['bg', 'surface', 'raised']) {
        expect(contrast(t[fg], t[bg]), `${fg} on ${bg}`).toBeGreaterThanOrEqual(4.5)
      }
    }
  })

  test(`on-colours reach 4.5:1 on their fills (${mode})`, () => {
    const t = parseTokens(CSS, mode)
    for (const s of STATES) {
      expect(contrast(t[`on-${s}`], t[s]), `on-${s} on ${s}`).toBeGreaterThanOrEqual(4.5)
    }
    expect(contrast(t['on-ink'], t.ink), 'on-ink on ink').toBeGreaterThanOrEqual(4.5)
  })
}

test('dark tokens are identical in the media query and the data-theme block', () => {
  const media = declarations(block(CSS, /:root:not\(\[data-theme="light"\]\)\s*\{/))
  const attr = declarations(block(CSS, /:root\[data-theme="dark"\]\s*\{/))
  expect(media).toEqual(attr)
})
