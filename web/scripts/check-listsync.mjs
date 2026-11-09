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

for (const m of v.tag_merges) {
  check(m.what, canon(mergeTags(m.a, m.b)), m.merged)
  check(`${m.what} (other way round)`, canon(mergeTags(m.b, m.a)), m.merged)
}
for (const m of v.tag_tidies) check(m.what, canon(tidyTags(m.doc, new Set(m.listed), m.now)), m.tidied)
check('no tags to merge', canon(mergeTags(emptyTags(), emptyTags())), canon(emptyTags()))

if (bad.length) { console.error(bad.join('\n')); process.exit(1) }
console.log(`listsync: ${v.keys.length} keys, ${v.passwords.length} passwords, ${v.merges.length} merges, ${v.tag_merges.length} tag merges and ${v.tag_tidies.length} tidies match`)
