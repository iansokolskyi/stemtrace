// Fails when a font size sits outside the six-step type scale in src/index.css
// (meta 11, small 12, body 13, title 15, page 22, figure 26 px).
//
// It rejects Tailwind's removed default sizes (`text-xs`, `text-2xl`, ...), arbitrary sizes
// (`text-[13px]`, `text-[length:...]`), and numeric `fontSize` props or CSS `font-size` /
// `font` shorthand values that are not on the scale. Colour utilities such as
// `text-[var(--on)]` are left alone.
//
// Usage: node scripts/check-type-scale.mjs [dir ...]   (default: src)
import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join, relative } from 'node:path'

const SCALE = new Set([11, 12, 13, 15, 22, 26])
const SOURCE = /\.(tsx?|css)$/

const CHECKS = [
  {
    what: 'a Tailwind size outside the scale',
    pattern: /(?<![\w-])text-(?:xs|sm|base|lg|xl|[2-9]xl)(?![\w-])/g,
  },
  {
    what: 'an arbitrary text size',
    pattern: /(?<![\w-])text-\[(?:length:)?-?\.?\d[^\]]*\]/g,
  },
  {
    what: 'a fontSize prop off the scale',
    pattern: /fontSize=\{(\d+(?:\.\d+)?)\}/g,
    off: (match) => !SCALE.has(Number(match[1])),
  },
  {
    what: 'a CSS font size off the scale',
    pattern: /font(?:-size)?:[^;{}\n]*?(\d+(?:\.\d+)?)(?:px|rem|em)\b/g,
    off: (match) => !SCALE.has(Number(match[1])) || /rem|em/.test(match[0]),
  },
]

function walk(dir) {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name)
    return statSync(path).isDirectory() ? walk(path) : [path]
  })
}

const roots = process.argv.slice(2)
const files = (roots.length ? roots : ['src']).flatMap(walk).filter((f) => SOURCE.test(f))
const problems = []

for (const file of files) {
  if (file.endsWith('routeTree.gen.ts')) continue
  const lines = readFileSync(file, 'utf8').split('\n')
  lines.forEach((line, index) => {
    for (const { what, pattern, off } of CHECKS) {
      for (const match of line.matchAll(pattern)) {
        if (off && !off(match)) continue
        problems.push(`${relative(process.cwd(), file)}:${index + 1}: ${what}: ${match[0]}`)
      }
    }
  })
}

if (problems.length > 0) {
  console.error(problems.join('\n'))
  console.error(
    `\n${problems.length} font size(s) outside the type scale. Use text-meta, text-small, text-body, text-title, text-page or text-figure.`,
  )
  process.exit(1)
}
