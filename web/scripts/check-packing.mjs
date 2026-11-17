// Checks web/src/packing.ts against scripts/packing-vectors.json, the cases scripts/check_packing.py
// checks texloom/packing.py against: compressed updates made by either app are read, and anything
// that isn't one (cut short, corrupted, something after it) is left as it is.
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { build } from 'vite'
import * as Y from 'yjs'

const web = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const out = path.join(web, 'node_modules/.cache/check-packing')
await build({ configFile: false, root: web, logLevel: 'warn', build: { ssr: 'src/packing.ts', outDir: out, emptyOutDir: true, minify: false, rollupOptions: { output: { entryFileNames: 'packing.mjs' } } } })
const { pack, unpack } = await import(pathToFileURL(path.join(out, 'packing.mjs')).href)
const v = JSON.parse(fs.readFileSync(path.join(web, '../scripts/packing-vectors.json'), 'utf8'))
const hex = (u) => Buffer.from(u).toString('hex')
const bytes = (h) => new Uint8Array(Buffer.from(h, 'hex'))
const bad = []
for (const c of v.cases) {
  const got = hex(await unpack(bytes(c.data)))
  if (got !== c.update) bad.push(`${c.what}: got ${got.length / 2} bytes, want ${c.update.length / 2}`)
}
// the update the packed cases hold is a real document's (made by the desktop's pycrdt)
const doc = new Y.Doc()
Y.applyUpdate(doc, await unpack(bytes(v.cases[1].data)))
if (doc.getText('main.tex').toString() !== v.text) bad.push("the update packed by the desktop app doesn't give the document back")
for (const n of [0, 1, 1000, 3_000_000]) {
  const u = new Uint8Array(n)
  for (let i = 0; i < n / 2; i++) u[i] = (i * 2654435761) >>> 24
  if (hex(await unpack(await pack(u))) !== hex(u)) bad.push(`pack then unpack changes ${n} bytes`)
}
console.log(bad.join('\n') || `packing: all ${v.cases.length} cases and the round trips agree`)
process.exit(bad.length ? 1 : 0)
