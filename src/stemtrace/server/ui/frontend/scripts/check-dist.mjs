// Fails the build when dist/ would break under a mount prefix, references a file it doesn't
// ship, or calls a font CDN.
//
// The server rewrites "/assets/" URLs only inside index.html (server/ui/static.py), so a
// root-absolute url(/assets/...) inside a CSS file 404s when the UI is served under a
// prefix such as /stemtrace or --root-path. Every other url() in a stylesheet must point
// at a file in dist/ (Vite leaves URLs it can't resolve unchanged, with only a warning).
// The UI must also make no runtime request to Google Fonts.
import { existsSync, readdirSync, readFileSync, statSync } from 'node:fs'
import { dirname, join, relative } from 'node:path'

const dist = new URL('../dist/', import.meta.url).pathname
const FONT_CDNS = ['fonts.googleapis.com', 'fonts.gstatic.com']

function walk(dir) {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name)
    return statSync(path).isDirectory() ? walk(path) : [path]
  })
}

const problems = []
for (const file of walk(dist)) {
  if (!/\.(html|css|js)$/.test(file)) continue
  const text = readFileSync(file, 'utf8')
  const name = relative(dist, file)
  if (file.endsWith('.css')) {
    for (const [, raw] of text.matchAll(/url\(\s*['"]?([^'")]+)['"]?\s*\)/g)) {
      if (/^(data:|#)/.test(raw)) continue
      if (raw.startsWith('/')) {
        problems.push(`${name}: root-absolute url(${raw}) breaks under a mount prefix`)
      } else if (!existsSync(join(dirname(file), raw.split(/[?#]/)[0]))) {
        problems.push(`${name}: url(${raw}) is not in dist/`)
      }
    }
  }
  for (const host of FONT_CDNS) {
    if (text.includes(host)) problems.push(`${name}: references ${host}`)
  }
}

if (problems.length) {
  console.error(problems.join('\n'))
  process.exit(1)
}
console.log('dist check passed')
