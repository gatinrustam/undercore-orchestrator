// Fail the build if the output includes pages outside the public Markdown tree.
import { readdir, readFile } from 'node:fs/promises'
import { resolve, relative, join } from 'node:path'

const source = resolve('docs/public')
const output = join(source, '.vitepress/dist')
async function files(root) {
  const result = []
  for (const entry of await readdir(root, { withFileTypes: true })) {
    if (entry.name.startsWith('.')) continue
    const path = join(root, entry.name)
    if (entry.isDirectory()) result.push(...await files(path))
    else result.push(path)
  }
  return result
}
const expected = new Set(['404.html'])
for (const file of await files(source)) {
  if (file.endsWith('.md')) expected.add(relative(source, file).replace(/\.md$/, '.html'))
}
const built = await files(output)
const actual = new Set(built.filter(file => file.endsWith('.html')).map(file => relative(output, file)))
if (actual.size !== expected.size || [...expected].some(page => !actual.has(page))) {
  throw new Error('Documentation output must contain exactly the public Markdown pages and 404')
}
for (const file of built) {
  if (/\.(md|sqlite3|db|token|key|p8)$/.test(file)) throw new Error('Unexpected source/state file in documentation output')
}
const home = await readFile(join(output, 'index.html'), 'utf8')
if (!home.includes('/orchestrator/assets/')) throw new Error('Missing GitHub Pages base path')
console.log(`Verified ${actual.size - 1} public pages; no internal pages or source/state files`)
