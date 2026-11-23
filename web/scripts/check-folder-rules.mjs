// Checks web/src/foldersync.ts's rules against scripts/folder-rules.json, the cases
// scripts/check_folder_rules.py checks texloom/folder.py against: which files are the project's,
// which are text, merging a file saved while the project changed, how long a changed file
// settles, Windows line endings, and renames.
//
//     npm run check-folder-rules
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { build } from 'vite'

const web = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const out = path.join(web, 'node_modules/.cache/check-folder-rules')
Object.defineProperty(globalThis, 'localStorage', { value: { getItem: () => null, setItem() {}, removeItem() {} }, configurable: true })
const load = async (entry) => {
  const name = path.basename(entry, '.ts') + '.mjs'
  await build({ configFile: false, root: web, logLevel: 'warn', build: { ssr: entry, outDir: out, emptyOutDir: false, minify: false, rollupOptions: { output: { entryFileNames: name } } } })
  return import(pathToFileURL(path.join(out, name)).href)
}
const { syncedPath, pickMoves, settleFor, crlf, onDisk } = await load('src/foldersync.ts')
const { isText } = await load('src/projectfiles.ts')
const { merge3 } = await load('src/diff.ts')

const v = JSON.parse(fs.readFileSync(path.join(web, '../scripts/folder-rules.json'), 'utf8'))
const bytes = (h) => new Uint8Array(Buffer.from(h, 'hex'))
const hex = (u) => Buffer.from(u).toString('hex')
const sorted = (o) => JSON.stringify(Object.fromEntries(Object.entries(o).sort(([a], [b]) => (a < b ? -1 : 1))))
const bad = []
const check = (kind, what, got, want) => {
  const same = typeof got === 'object' && got ? sorted(got) === sorted(want) : got === want
  if (!same) bad.push(`${kind}: ${what}: got ${JSON.stringify(got)}, want ${JSON.stringify(want)}`)
}

for (const c of v.synced) check('synced', `${c.path} (main ${c.main})`, syncedPath(c.path, c.main), c.want)
for (const c of v.text) {
  const data = 'size' in c ? new Uint8Array(c.size).fill(97) : bytes(c.hex)
  check('text', c.path, isText(c.path, data) !== null, c.want)
}
for (const c of v.merge) check('merge', c.what, merge3(c.base, c.theirs, c.ours), c.want)
for (const c of v.settle) check('settle', `${c.size} bytes, ${c.times} times`, Math.round(settleFor(c.size, c.times)) / 1000, c.want)
for (const c of v.crlf) check('crlf', c.hex, crlf(bytes(c.hex)), c.want)
for (const c of v.onDisk) check('onDisk', `${JSON.stringify(c.text)} windows=${c.windows}`, hex(onDisk(c.text, c.windows)), c.want)
for (const c of v.moves) check('moves', c.what, pickMoves(c.gone, c.new), c.want)

const n = Object.values(v).filter(Array.isArray).reduce((t, x) => t + x.length, 0)
console.log(bad.join('\n') || `folder rules (web): all ${n} cases agree`)
process.exit(bad.length ? 1 : 0)
