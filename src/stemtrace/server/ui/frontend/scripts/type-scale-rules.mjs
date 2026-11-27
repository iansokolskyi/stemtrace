// The rules behind `npm run check`'s type-scale guard. Only six font sizes exist
// (meta 11, small 12, body 13, title 15, page 22, figure 26 px, set in src/index.css).
//
// Every way of setting a size is covered: Tailwind classes (named, arbitrary, `length:`), JSX
// and object `fontSize` (number or string, literal only), CSS and canvas `font` / `font-size`
// in any unit or keyword, and `var(--text-*)` names that are not the six steps.

export const SCALE_PX = new Set([11, 12, 13, 15, 22, 26])
export const SCALE_NAMES = ['meta', 'small', 'body', 'title', 'page', 'figure']

const KEYWORDS =
  /^(xx-small|x-small|small|smaller|medium|large|larger|x-large|xx-large|xxx-large|math)$/
const SIZE_TOKEN = /^(\d*\.?\d+)(px|rem|em|pt|pc|cm|mm|in|%|vw|vh|vmin|vmax|ch|ex)?$/

/** True when a CSS length is exactly one of the six pixel sizes. */
function isScalePx(value) {
  const match = SIZE_TOKEN.exec(value.trim())
  return Boolean(match) && (match[2] === 'px' || match[2] === undefined) && SCALE_PX.has(Number(match[1]))
}

/** True when a `font-size` value is a scale size, a scale variable or `inherit`. */
function isAllowedFontSize(value) {
  const v = value.trim().replace(/\s*!important$/, '')
  if (v === 'inherit') return true
  if (isScalePx(v)) return true
  const variable = /^var\(--text-([a-z]+)\)$/.exec(v)
  return Boolean(variable) && SCALE_NAMES.includes(variable[1])
}

/** Size of a `font` shorthand value, or null when it has none (such as `inherit`). */
function shorthandSize(value) {
  for (const token of value.trim().split(/\s+/)) {
    const size = token.split('/')[0]
    if (KEYWORDS.test(size)) return size
    const match = SIZE_TOKEN.exec(size)
    // A bare number is a font weight; a size always has a unit
    if (match && match[2]) return size
    if (/^var\(--text-/.test(size)) return size
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

  // JS and JSX: fontSize as a prop or key
  for (const m of line.matchAll(/(?<![\w-])fontSize\s*[=:]\s*\{?\s*([^,}>]*)/g)) {
    const value = m[1]
      .trim()
      .replace(/\/$/, '')
      .trim()
      .replace(/^['"`]|['"`]$/g, '')
    if (!isScalePx(value)) found.push([m[0].trim(), 'a fontSize off the scale'])
  }
  // Quoted CSS property names in objects: 'font-size': ...
  for (const m of line.matchAll(/['"]font-size['"]\s*:\s*([^,}]+)/g)) {
    if (!isAllowedFontSize(m[1].trim().replace(/^['"`]|['"`]$/g, ''))) {
      found.push([m[0].trim(), 'a font-size off the scale'])
    }
  }

  // CSS (also inside strings): font-size and the font shorthand
  for (const m of line.matchAll(/(?<![\w-])font-size\s*:\s*([^;}\n'"`]+)/g)) {
    if (!isAllowedFontSize(m[1])) found.push([m[0].trim(), 'a font-size off the scale'])
  }
  for (const m of line.matchAll(/(?<![\w-])font\s*:\s*([^;}\n]+)/g)) {
    const value = m[1].replace(/['"`,]+$/, '')
    const size = shorthandSize(value)
    if (size !== null && !isAllowedFontSize(size)) {
      found.push([m[0].trim(), 'a font shorthand size off the scale'])
    }
  }
  // Canvas: ctx.font = '10px sans-serif'
  for (const m of line.matchAll(/\.font\s*=\s*['"`]([^'"`]*)/g)) {
    const size = shorthandSize(m[1])
    if (size !== null && !isAllowedFontSize(size)) {
      found.push([m[0].trim(), 'a canvas font size off the scale'])
    }
  }

  // Any var(--text-*) that is not one of the six steps
  for (const m of line.matchAll(/var\(--text-([a-z0-9-]+)\)/g)) {
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
