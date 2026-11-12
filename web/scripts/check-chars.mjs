// Checks that edits never cut a character outside the BMP (an emoji) in two: the editor's filter
// (src/wholechars.ts), setText (src/diff.ts) on a shared text seen from another copy, and that a
// half left in a text is taken out (keepLF in src/projectfiles.ts takes LONE out the same way).
// Half a character becomes "�" in Yjs and can't exist in the desktop app's copy at all.
import path from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { build } from 'vite'
import * as Y from 'yjs'
import { EditorState } from '@codemirror/state'

const web = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const out = path.join(web, 'node_modules/.cache/check-chars')
for (const [entry, name] of [['src/wholechars.ts', 'wholechars.mjs'], ['src/diff.ts', 'diff.mjs']]) {
  await build({ configFile: false, root: web, logLevel: 'warn', build: { ssr: entry, outDir: out, emptyOutDir: false, minify: false, rollupOptions: { output: { entryFileNames: name } } } })
}
const { wholeCharacters } = await import(pathToFileURL(path.join(out, 'wholechars.mjs')).href)
const { setText } = await import(pathToFileURL(path.join(out, 'diff.mjs')).href)

const bad = []
const check = (what, got, want) => { if (got !== want) bad.push(`${what}: got ${JSON.stringify(got)}, want ${JSON.stringify(want)}`) }
const whole = (s) => !/[\ud800-\udbff](?![\udc00-\udfff])|(?<![\ud800-\udbff])[\udc00-\udfff]/.test(s)

// the editor's filter
const st = EditorState.create({ doc: 'a😀b', extensions: [EditorState.transactionFilter.of(wholeCharacters)] })
const run = (spec) => st.update(spec).state.doc.toString()
check('deleting the second half of 😀 deletes it', run({ changes: { from: 2, to: 3 } }), 'ab')
check('deleting the first half of 😀 deletes it', run({ changes: { from: 1, to: 2 } }), 'ab')
check('changing only the second half gives 😁', run({ changes: { from: 2, to: 3, insert: '\ude01' } }), 'a😁b')
check('typing between the halves keeps 😀 whole', whole(run({ changes: { from: 2, insert: 'X' } })), true)
check('other edits untouched', run({ changes: { from: 0, to: 1, insert: 'Z' } }), 'Z😀b')

// random edits through the filter never leave half a character
let seed = 20261112
const rnd = (n) => { seed = (seed * 1664525 + 1013904223) >>> 0; return Math.floor(seed / 2 ** 32 * n) }
let s = EditorState.create({ doc: '😀 café 😁 naïve 🎉 x', extensions: [EditorState.transactionFilter.of(wholeCharacters)] })
for (let i = 0; i < 3000; i++) {
  const len = s.doc.length, from = rnd(len + 1), to = Math.min(len, from + rnd(3))
  s = s.update({ changes: { from, to, insert: ['', '😀', 'a', '\ude00', '🎉b'][rnd(5)] } }).state
  if (!whole(s.doc.toString())) { bad.push(`random edit ${i} left half a character: ${JSON.stringify(s.doc.toString())}`); break }
}

// setText on a shared text, as another copy sees it
for (const [a, b] of [['face 😀 here', 'face 😁 here'], ['😀', '😁'], ['x😀😀y', 'x😀😁y'], ['ab', 'a😀b'], ['😀b', 'b']]) {
  const d1 = new Y.Doc(), d2 = new Y.Doc(), t1 = d1.getText('t')
  t1.insert(0, a); Y.applyUpdate(d2, Y.encodeStateAsUpdate(d1))
  const sv = Y.encodeStateVector(d2)
  setText(t1, b); Y.applyUpdate(d2, Y.encodeStateAsUpdate(d1, sv))
  check(`setText ${a} → ${b}, here`, t1.toString(), b)
  check(`setText ${a} → ${b}, on another copy`, d2.getText('t').toString(), b)
}

if (bad.length) { console.error('chars: FAILED\n  ' + bad.join('\n  ')); process.exit(1) }
console.log('chars: all checks passed')
