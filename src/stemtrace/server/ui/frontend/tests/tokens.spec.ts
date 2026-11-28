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

type Matrix = number[][]

/** Machado et al. 2009, severity 1.0, applied to linear RGB. */
const PROTANOPIA: Matrix = [
  [0.152286, 1.052583, -0.204868],
  [0.114503, 0.786281, 0.099216],
  [-0.003882, -0.048116, 1.051998],
]
const DEUTERANOPIA: Matrix = [
  [0.367322, 0.860646, -0.227968],
  [0.280085, 0.672501, 0.047413],
  [-0.01182, 0.04294, 0.968881],
]

function linear(hex: string): number[] {
  return [1, 3, 5].map((i) => {
    const c = Number.parseInt(hex.slice(i, i + 2), 16) / 255
    return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4
  })
}

function lab([r, g, b]: number[]): number[] {
  const f = (v: number): number => (v > 0.008856 ? v ** (1 / 3) : 7.787 * v + 16 / 116)
  const x = f((0.4124 * r + 0.3576 * g + 0.1805 * b) / 0.95047)
  const y = f(0.2126 * r + 0.7152 * g + 0.0722 * b)
  const z = f((0.0193 * r + 0.1192 * g + 0.9505 * b) / 1.08883)
  return [116 * y - 16, 500 * (x - y), 200 * (y - z)]
}

/** CIE76 colour difference between two #rrggbb colours as a colour-blind viewer sees them. */
export function deltaE(a: string, b: string, matrix: Matrix): number {
  const see = (hex: string): number[] => {
    const rgb = linear(hex)
    return matrix.map((row) =>
      Math.min(
        1,
        Math.max(
          0,
          row.reduce((sum, m, i) => sum + m * rgb[i], 0),
        ),
      ),
    )
  }
  const [la, lb] = [lab(see(a)), lab(see(b))]
  return Math.hypot(la[0] - lb[0], la[1] - lb[1], la[2] - lb[2])
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
    // Text on a quiet fill is ink (V1a).
    expect(contrast(t.ink, t['success-quiet']), 'ink on success-quiet').toBeGreaterThanOrEqual(4.5)
  })

  test(`the quiet success edge keeps 3:1 on every background it sits on (${mode})`, () => {
    const t = parseTokens(CSS, mode)
    // WCAG 1.4.11: the fill is a tint, so the 1.5 px edge (--e = --success-t) is the graphical
    // object. It sits on surface (chart, bars), raised (timing track, flow containers), band, the
    // ground, and Gantt rows selected (ink 11%) or focused (ink 16%) over the surface. Hover is
    // transient and left out.
    const mix = (fg: string, bg: string, alpha: number): string =>
      `#${[1, 3, 5]
        .map((i) => {
          const c = Number.parseInt(fg.slice(i, i + 2), 16) * alpha
          const d = Number.parseInt(bg.slice(i, i + 2), 16) * (1 - alpha)
          return Math.round(c + d)
            .toString(16)
            .padStart(2, '0')
        })
        .join('')}`
    const backgrounds: Record<string, string> = {
      surface: t.surface,
      raised: t.raised,
      band: t.band,
      ground: t.bg,
      'selected row': mix(t.ink, t.surface, 0.11),
      'focused row': mix(t.ink, t.surface, 0.16),
    }
    for (const [name, bg] of Object.entries(backgrounds)) {
      expect(contrast(t['success-t'], bg), `success-t edge on ${name}`).toBeGreaterThanOrEqual(3)
    }
    expect(contrast(t.failure, t.surface), 'failure on surface').toBeGreaterThanOrEqual(3)
  })

  test(`quiet success and failure stay apart for colour-blind viewers (${mode})`, () => {
    const t = parseTokens(CSS, mode)
    // V1: they differ in lightness, not only hue, and in simulated protanopia and deuteranopia.
    expect(contrast(t['success-quiet'], t.failure), 'luminance contrast').toBeGreaterThanOrEqual(
      1.5,
    )
    for (const [name, matrix] of [
      ['protanopia', PROTANOPIA],
      ['deuteranopia', DEUTERANOPIA],
    ] as const) {
      expect(
        deltaE(t['success-quiet'], t.failure, matrix),
        `${name} dE76 between quiet success and failure`,
      ).toBeGreaterThanOrEqual(20)
    }
  })

  test(`the header band is a tint of the surface that keeps column headers readable (${mode})`, () => {
    const t = parseTokens(CSS, mode)
    expect(t.band).not.toBe(t.surface)
    expect(contrast(t.muted, t.band), 'muted on band').toBeGreaterThanOrEqual(4.5)
  })

  test(`success and failure keep one hue in both themes (${mode})`, () => {
    const t = parseTokens(CSS, mode)
    expect(t.success).toBe('#30a46c')
    expect(t.failure).toBe('#e5484d')
  })
}

test('dark tokens are identical in the media query and the data-theme block', () => {
  const media = declarations(block(CSS, /:root:not\(\[data-theme="light"\]\)\s*\{/))
  const attr = declarations(block(CSS, /:root\[data-theme="dark"\]\s*\{/))
  expect(media).toEqual(attr)
})
