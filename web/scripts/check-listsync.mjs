// Checks web/src/listcore.ts against scripts/listsync-vectors.json, the data that
// scripts/check_listsync.py checks the Python against.
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { build } from 'vite'

const web = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const out = path.join(web, 'node_modules/.cache/check-listsync')
await build({ configFile: false, root: web, logLevel: 'warn', build: { ssr: 'src/listcore.ts', outDir: out, emptyOutDir: true, minify: false, rollupOptions: { output: { entryFileNames: 'listcore.mjs' } } } })
const { ListKey, canon, emptyTags, mergeLists, mergeTags, passwordSecret, tidyTags } = await import(pathToFileURL(path.join(out, 'listcore.mjs')).href)
const v = JSON.parse(fs.readFileSync(path.join(web, '../scripts/listsync-vectors.json'), 'utf8'))
const b64d = (s) => new Uint8Array(Buffer.from(s, 'base64url'))
const b64e = (b) => Buffer.from(b).toString('base64url')
const bad = []
const check = (what, got, want) => { if (got !== want) bad.push(`${what}: got ${got}, want ${want}`) }
for (const k of v.keys) {
  const key = await ListKey.of(b64d(k.secret))
  check('id', key.id, k.id)
  check('signature', key.signPut(k.seq, b64d(k.data)), k.signature)
  check('decrypt', new TextDecoder().decode(await key.decrypt(b64d(k.ciphertext))), k.plain)
}
for (const p of v.passwords) check(`password ${p.username}`, b64e(await passwordSecret(p.username, p.password)), p.secret)
for (const m of v.merges) {
  check(m.what, canon(mergeLists(m.a, m.b)), m.merged)
  check(`${m.what} (other way round)`, canon(mergeLists(m.b, m.a)), m.merged)
}

// Tags aren't in the Python client yet, so these are checked here rather than in the shared file.
const T = (tags, on = {}) => ({ v: 1, tags, on })
const tagCases = [
  ['the latest rename wins', T({ a: { n: 'Old', at: 1 } }), T({ a: { n: 'New', at: 2 } }), T({ a: { n: 'New', at: 2 } })],
  ['deleting beats a rename at the same moment', T({ a: { n: 'X', at: 2 } }), T({ a: { n: '', at: 2, gone: true } }), T({ a: { n: '', at: 2, gone: true } })],
  ['assign and rename on two devices both survive', T({ a: { n: 'New', at: 5 } }, { p: { a: 1 } }), T({ a: { n: 'Old', at: 1 } }, { p: { a: 9 } }), T({ a: { n: 'New', at: 5 } }, { p: { a: 9 } })],
  ['taking a tag off later wins', T({ a: { n: 'A', at: 1 } }, { p: { a: 5 } }), T({ a: { n: 'A', at: 1 } }, { p: { a: -7 } }), T({ a: { n: 'A', at: 1 } }, { p: { a: -7 } })],
  ['assigning beats taking off at the same moment', T({}, { p: { a: 5 } }), T({}, { p: { a: -5 } }), T({}, { p: { a: 5 } })],
  ['the same name made twice is one tag', T({ a: { n: 'AAAI', at: 1 } }, { p: { a: 3 } }), T({ b: { n: 'aaai ', at: 2 } }, { q: { b: 4 } }), T({ a: { n: 'AAAI', at: 1 }, b: { n: '', at: 2, gone: true } }, { p: { a: 3 }, q: { a: 4 } })],
]
for (const [what, a, b, want] of tagCases) {
  check(what, canon(mergeTags(a, b)), canon(want))
  check(`${what} (other way round)`, canon(mergeTags(b, a)), canon(want))
  // an older copy can bring back a mark for a merged tag: hidden, and gone at the next tidy
  const all = new Set(['p', 'q'])
  check(`${what} (merged again)`, canon(tidyTags(mergeTags(mergeTags(a, b), b), all)), canon(tidyTags(want, all)))
}
const now = Date.now()
check('tidying', canon(tidyTags(T({ a: { n: 'A', at: 1 }, b: { n: '', at: now, gone: true }, c: { n: '', at: 1, gone: true } }, { p: { a: 5, b: now, d: now }, q: { a: 5 }, r: { a: -1 } }), new Set(['p', 'r']))), canon(T({ a: { n: 'A', at: 1 }, b: { n: '', at: now, gone: true } }, { p: { a: 5 } })))
check('no tags to merge', canon(mergeTags(emptyTags(), emptyTags())), canon(emptyTags()))

if (bad.length) { console.error(bad.join('\n')); process.exit(1) }
console.log(`listsync: ${v.keys.length} keys, ${v.passwords.length} passwords, ${v.merges.length} merges and ${tagCases.length} tag merges match`)
