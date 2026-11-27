// Fails when a font size sits outside the six-step type scale in src/index.css
// (meta 11, small 12, body 13, title 15, page 22, figure 26 px). The rules live in
// type-scale-rules.mjs and are tested against tests/fixtures/type-scale-mutants.json.
//
// Usage: node scripts/check-type-scale.mjs [dir ...]   (default: src)
import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join, relative } from 'node:path'

import { sourceProblems } from './type-scale-rules.mjs'

const SOURCE = /\.(tsx?|css)$/

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
  for (const problem of sourceProblems(readFileSync(file, 'utf8'))) {
    problems.push(`${relative(process.cwd(), file)}: ${problem}`)
  }
}

if (problems.length > 0) {
  console.error(problems.join('\n'))
  console.error(
    `\n${problems.length} font size(s) outside the type scale. Use text-meta, text-small, text-body, text-title, text-page or text-figure.`,
  )
  process.exit(1)
}
