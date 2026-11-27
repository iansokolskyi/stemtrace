// The rules behind `npm run check`'s type-scale guard. Only six font sizes exist
// (meta 11, small 12, body 13, title 15, page 22, figure 26 px, set in src/index.css).
//
// Checked, one line at a time (the fixture in tests/fixtures/type-scale-mutants.json shows each):
// Tailwind size classes; `fontSize` and `font-size` as JSX props, SVG attributes and object keys
// (quoted or not); `font` and `font-size` values in CSS, style objects and canvas strings, in any
// case, unit or keyword; and `--text-*` declarations and `var(--text-*)` uses.
// Not covered: a declaration split over several lines, and computed keys such as
// `{ ['fontSize']: 10 }` or `style.setProperty(...)`.

export const SCALE_PX = { meta: 11, small: 12, body: 13, title: 15, page: 22, figure: 26 }
const SCALE_SIZES = new Set(Object.values(SCALE_PX))
const SCALE_NAMES = Object.keys(SCALE_PX)

const KEYWORDS =
  /^(xx-small|x-small|small|smaller|medium|large|larger|x-large|xx-large|xxx-large|math)$/
const SYSTEM_FONTS = /^(caption|icon|menu|message-box|small-caption|status-bar)$/
const SIZE_TOKEN = /^(\d*\.?\d+)(px|rem|em|pt|pc|cm|mm|in|%|vw|vh|vmin|vmax|ch|ex)?$/
const MATH = /\b(calc|clamp|min|max)\(/

/** Lower-cased value without quotes, trailing `;` or `,`, and a self-closing `/`. */
function norm(value) {
  return value
    .replace(/['"`]/g, '')
    .trim()
    .replace(/[;,]+$/, '')
    .replace(/\/$/, '')
    .trim()
    .toLowerCase()
}

/** True when a CSS length is exactly one of the six pixel sizes. */
function isScalePx(value) {
  const match = SIZE_TOKEN.exec(value)
  return Boolean(match) && (match[2] === 'px' || match[2] === undefined) && SCALE_SIZES.has(Number(match[1]))
}

/** True when a `font-size` value is a scale size, a scale variable or `inherit`. */
function isAllowedFontSize(value) {
  const v = value.replace(/\s*!important$/, '')
  if (v === 'inherit' || isScalePx(v)) return true
  const variable = /^var\(--text-([a-z]+)\)$/.exec(v)
  return Boolean(variable) && SCALE_NAMES.includes(variable[1])
}

/** Size of a `font` shorthand value, or null when it has none (such as `inherit`). */
function shorthandSize(value) {
  if (MATH.test(value)) return value
  for (const token of value.split(/\s+/)) {
    const size = token.split('/')[0]
    if (KEYWORDS.test(size) || SYSTEM_FONTS.test(size)) return size
    const match = SIZE_TOKEN.exec(size)
    // A bare number is a font weight; a size always has a unit
    if ((match && match[2]) || /^var\(--text-/.test(size)) return size
  }
  return null
}

const COLOUR_ARBITRARY = /^(color:|#|rgb|hsl|oklch|oklab|color-mix|var\(--(?!text))/

/** One line of source: the offending snippets, each with what is wrong. */
export function lineProblems(line) {
  const found = []

  // Tailwind: removed default names
  for (const m of line.matchAll(/(?<![\w-])text-(?:xs|sm|base|lg|xl|[2-9]xl)(?![\w-])/g)) {
    found.push([m[0], 'a Tailwind size outside the scale'])
  }
  // Tailwind: `text-[...]` that is not a colour, and `text-(length:...)`
  for (const m of line.matchAll(/(?<![\w-])text-\[((?:[^\][]|\[[^\]]*\])*)\]/g)) {
    if (!COLOUR_ARBITRARY.test(m[1])) found.push([m[0], 'an arbitrary text size'])
  }
  for (const m of line.matchAll(/(?<![\w-])text-\((?:length:)[^)]*\)/g)) {
    found.push([m[0], 'an arbitrary text size'])
  }

  // JS and JSX: fontSize as a prop or key, quoted or not
  for (const m of line.matchAll(/(?<![\w-])['"]?fontSize['"]?\s*[=:]\s*\{?\s*([^,}>]*)/gi)) {
    if (!isScalePx(norm(m[1]))) found.push([m[0].trim(), 'a fontSize off the scale'])
  }

  // CSS, style objects and SVG attributes: font-size
  for (const m of line.matchAll(/(?<![\w-])['"]?font-size['"]?\s*[:=]\s*\{?\s*([^;}\n,>]+)/gi)) {
    if (!isAllowedFontSize(norm(m[1]))) found.push([m[0].trim(), 'a font-size off the scale'])
  }

  // The font shorthand (CSS, style objects) and canvas `.font =`
  for (const m of line.matchAll(/(?<![\w-])['"]?font['"]?\s*[:=]\s*([^;}\n]+)/gi)) {
    const size = shorthandSize(norm(m[1]))
    if (size !== null && !isAllowedFontSize(size)) {
      found.push([m[0].trim(), 'a font shorthand size off the scale'])
    }
  }

  // --text-* declarations: only the six steps, each at its own pixel size
  for (const m of line.matchAll(/(?<![\w-])--text-([\w*-]+?)(--[\w-]+)?['"]?\s*:\s*([^;}\n]+)/gi)) {
    const [, name, suffix, value] = m
    const ok =
      name === '*'
        ? norm(value) === 'initial'
        : SCALE_NAMES.includes(name) &&
          (suffix ? suffix === '--line-height' : norm(value) === `${SCALE_PX[name]}px`)
    if (!ok) found.push([m[0].trim(), 'a --text-* declaration off the scale'])
  }
  // Any var(--text-*) that is not one of the six steps
  for (const m of line.matchAll(/var\(--text-([a-z0-9]+)(--[\w-]+)?\)/gi)) {
    if (!SCALE_NAMES.includes(m[1])) found.push([m[0], 'a text size variable off the scale'])
  }
  return found
}

/** Problems in a source text as `line N: what: snippet` strings. */
export function sourceProblems(text) {
  return text
    .split('\n')
    .flatMap((line, index) =>
      lineProblems(line).map(([snippet, what]) => `line ${index + 1}: ${what}: ${snippet}`),
    )
}
