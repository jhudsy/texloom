// Property checks (fast-check): random texts and edits, including Windows line endings, lone "\r",
// accents and emoji, against what must always hold. A failure prints the smallest case found and the
// seed to replay it (CHECK_SEED=<seed> npm run check-props).
//
// 1. The editor and the shared text stay identical under any edits, typed or arriving: the editor's
//    text settings (src/editortext.ts) with y-codemirror's own copying both ways mirrored step for
//    step (node_modules/y-codemirror.next/src/y-sync.js). Without lineSeparator this fails at once
//    on "\r\n": the bug behind scrambled text before 0.4.21.
// 2. Edits through the editor never leave half a character (src/wholechars.ts).
// 3. applyText and setText turn a shared text into exactly the target, as another copy sees it too.
// 4. merge3's laws: nothing changed on one side gives the other; both the same gives that; when
//    merge3Strict finds no clash it agrees with merge3.
// 5. Line endings (src/lineends.ts): lf leaves no "\r" and is idempotent; keepLF strips every "\r"
//    from a shared text and nothing else.
//
// With --same <cases.json> <answers.json>, writes the web app's results for cases scripts/check_props.py made, to
// check the two implementations of the "same rules" agree.
import path from 'node:path'
import { readFileSync, writeFileSync } from 'node:fs'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { build } from 'vite'
import * as Y from 'yjs'
import fc from 'fast-check'
import { EditorState } from '@codemirror/state'

const web = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const out = path.join(web, 'node_modules/.cache/check-props')
const load = async (entry) => {
  const name = path.basename(entry, '.ts') + '.mjs'
  await build({ configFile: false, root: web, logLevel: 'warn', build: { ssr: entry, outDir: out, emptyOutDir: false, minify: false, rollupOptions: { output: { entryFileNames: name } } } })
  return import(pathToFileURL(path.join(out, name)).href)
}
const { editorText } = await load('src/editortext.ts')
const { applyText, setText, merge3, merge3Strict } = await load('src/diff.ts')
const { lf, keepLF } = await load('src/lineends.ts')

if (process.argv[2] === '--same') { // cases from scripts/check_props.py: the web app's answers
  const cases = JSON.parse(readFileSync(process.argv[3], 'utf8'))
  const fns = { merge3: (b, t, o) => merge3(b, t, o), merge3Strict: (b, t, o) => merge3Strict(b, t, o), lf: (s) => lf(s) }
  writeFileSync(process.argv[4], JSON.stringify(cases.map(([fn, args]) => fns[fn](...args)))) // (piped output can be cut off at exit)
  process.exit(0)
}

const seed = Number(process.env.CHECK_SEED) || Date.now() % 2 ** 31
fc.configureGlobal({ seed, numRuns: Number(process.env.CHECK_RUNS) || 300 })
const LONE = /[\ud800-\udbff](?![\udc00-\udfff])|(?<![\ud800-\udbff])[\udc00-\udfff]/
const pieces = ['a', 'b', 'xyz', ' ', '\n', '\n', '\r\n', '\r\n', '\r', 'é', 'naïve', '😀', '𝒜', '\\bottomrule', '{l|rr|}', '%', '&']
const text = fc.array(fc.constantFrom(...pieces), { maxLength: 30 }).map((a) => a.join(''))
const edit = fc.record({ local: fc.boolean(), at: fc.nat(), len: fc.nat({ max: 6 }), ins: text })
const whole = (s, i) => (i > 0 && i < s.length && /[\udc00-\udfff]/.test(s[i]) && /[\ud800-\udbff]/.test(s[i - 1]) ? i - 1 : i)

/** One editor bound to a shared text, copying changes both ways exactly as y-codemirror does. */
function bound(initial) {
  const doc = new Y.Doc(), yt = doc.getText('t')
  yt.insert(0, initial)
  let state = EditorState.create({ doc: yt.toString(), extensions: editorText })
  const fromYjs = Symbol('y')
  yt.observe((event, tr) => { // y-sync.js: the observer turns a Yjs delta into editor changes
    if (tr.origin === fromYjs) return
    const changes = []
    let pos = 0
    for (const d of event.delta) {
      if (d.insert != null) changes.push({ from: pos, to: pos, insert: d.insert })
      else if (d.delete != null) { changes.push({ from: pos, to: pos + d.delete, insert: '' }); pos += d.delete } else pos += d.retain
    }
    state = state.update({ changes }).state
  })
  return {
    yt, state: () => state,
    type(from, to, ins) { // y-sync.js: update() copies the editor's changes into the Y.Text
      const tr = state.update({ changes: { from, to, insert: ins } })
      state = tr.state
      doc.transact(() => {
        let adj = 0
        tr.changes.iterChanges((fromA, toA, _fb, _tb, insert) => {
          const s = insert.sliceString(0, insert.length, '\n')
          if (fromA !== toA) yt.delete(fromA + adj, toA - fromA)
          if (s.length > 0) yt.insert(fromA + adj, s)
          adj += s.length - (toA - fromA)
        })
      }, fromYjs)
    },
    arrive(from, to, ins) { doc.transact(() => { if (to > from) yt.delete(from, to - from); if (ins) yt.insert(from, ins) }) }, // from a collaborator
  }
}

const checks = [
  ['the editor and the shared text stay identical, whatever is typed or arrives', fc.property(text, fc.array(edit, { maxLength: 25 }), (s, edits) => {
    const b = bound(s)
    if (b.state().doc.toString() !== b.yt.toString()) return false
    for (const e of edits) {
      const cur = b.yt.toString(), from = whole(cur, e.at % (cur.length + 1)), to = whole(cur, Math.min(cur.length, from + e.len))
      if (e.local) b.type(from, Math.max(from, to), e.ins); else b.arrive(from, Math.max(from, to), e.ins)
      if (b.state().doc.toString() !== b.yt.toString()) return false
    }
    return true
  })],
  ['edits through the editor never leave half a character', fc.property(text, fc.array(fc.record({ at: fc.nat(), len: fc.nat({ max: 4 }), ins: fc.constantFrom('', 'x', '😁', '\ude00', '\ud83d') }), { maxLength: 20 }), (s, edits) => {
    let st = EditorState.create({ doc: s, extensions: editorText })
    for (const e of edits) {
      const n = st.doc.length, from = e.at % (n + 1)
      st = st.update({ changes: { from, to: Math.min(n, from + e.len), insert: e.ins } }).state
      if (LONE.test(st.doc.toString()) && !LONE.test(s)) return false
    }
    return true
  })],
  ['applyText and setText give exactly the target, here and on another copy', fc.property(text, text, fc.boolean(), (a, b, useSet) => {
    const d1 = new Y.Doc(), d2 = new Y.Doc(), t1 = d1.getText('t')
    t1.insert(0, a); Y.applyUpdate(d2, Y.encodeStateAsUpdate(d1))
    const sv = Y.encodeStateVector(d2)
    if (useSet) setText(t1, b); else applyText(t1, a, b)
    Y.applyUpdate(d2, Y.encodeStateAsUpdate(d1, sv))
    return t1.toString() === b && d2.getText('t').toString() === b
  })],
  ['merge3: one side unchanged gives the other; both alike gives that', fc.property(text, text, (base, x) =>
    merge3(base, base, x) === x && merge3(base, x, base) === x && merge3(base, x, x) === x)],
  ['merge3Strict agrees with merge3 when it finds no clash', fc.property(text, text, text, (b, t, o) => {
    const strict = merge3Strict(b, t, o)
    return strict === null || strict === merge3(b, t, o)
  })],
  ['lf leaves no \\r and is idempotent', fc.property(text, (s) => !lf(s).includes('\r') && lf(lf(s)) === lf(s) && (s.includes('\r') || lf(s) === s))],
  ['keepLF takes every \\r out of the shared texts, and nothing else', fc.property(fc.array(text, { minLength: 1, maxLength: 3 }), (texts) => {
    const doc = new Y.Doc(), files = doc.getMap('files')
    texts.forEach((s, i) => files.set(`f${i}.tex`, new Y.Text(s)))
    keepLF({ keys: { writable: true }, deletedAt: null, files, doc, isRemote: () => false, isLocal: () => true })
    return texts.every((s, i) => files.get(`f${i}.tex`).toString() === s.replace(/\r/g, ''))
  })],
]

const bad = []
for (const [name, prop] of checks) {
  try { fc.assert(prop) } catch (e) { bad.push(`${name}\n    ${String(e.message).split('\n').slice(0, 6).join('\n    ')}`) }
}
if (bad.length) { console.error(`props: FAILED (seed ${seed})\n  ` + bad.join('\n  ')); process.exit(1) }
console.log(`props: all ${checks.length} properties hold (${fc.readConfigureGlobal().numRuns} cases each, seed ${seed})`)
