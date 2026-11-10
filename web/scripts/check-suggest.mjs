// Checks suggest mode's bookkeeping (src/suggestcore.ts) and word diffs (src/worddiff.ts) with
// random edits: what the editor shows must always be the shared text with the suggestions
// applied, saving and loading must give the same suggestions back, and accepting them all must
// turn the shared text into what was typed.
import path from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { build } from 'vite'
import * as Y from 'yjs'

const web = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const out = path.join(web, 'node_modules/.cache/check-suggest')
await build({ configFile: false, root: web, logLevel: 'warn', build: { ssr: 'src/suggestcore.ts', outDir: out, emptyOutDir: true, minify: false, rollupOptions: { output: { entryFileNames: 'suggestcore.mjs' } } } })
const core = await import(pathToFileURL(path.join(out, 'suggestcore.mjs')).href)
await build({ configFile: false, root: web, logLevel: 'warn', build: { ssr: 'src/worddiff.ts', outDir: out, emptyOutDir: false, minify: false, rollupOptions: { output: { entryFileNames: 'worddiff.mjs' } } } })
const { wordDiff } = await import(pathToFileURL(path.join(out, 'worddiff.mjs')).href)

const bad = []
const check = (what, ok) => { if (!ok && bad.length < 20) bad.push(what) }
let seed = 20261110
const rnd = (n) => { seed = (seed * 1664525 + 1013904223) >>> 0; return Math.floor(seed / 2 ** 32 * n) }
const WORDS = ['the', 'model', 'we', 'show', 'that', 'proof', 'lemma', '\\cite{a}', '$x$', 'colour', ',', '.', '\n', '  ', 'été', '证明', '😀', 'résumé']
const words = (n) => { let s = ''; for (let i = 0; i < n; i++) s += WORDS[rnd(WORDS.length)] + (rnd(3) ? ' ' : ''); return s }
// a position that doesn't split a character in two (editors never do)
const snap = (str, i) => (i > 0 && i < str.length && /[\udc00-\udfff]/.test(str[i]) && /[\ud800-\udbff]/.test(str[i - 1]) ? i - 1 : i)
const piece = () => { const k = rnd(4); return k === 0 ? '' : k === 1 ? 'abcdefxyz ,.'[rnd(12)] : words(1 + rnd(3)) }

// ---- word diffs ----
for (let i = 0; i < 2000; i++) {
  const a = words(rnd(30)), b = rnd(4) ? a.slice(0, rnd(a.length + 1)) + piece() + a.slice(rnd(a.length + 1)) : words(rnd(30))
  const ops = wordDiff(a, b)
  const before = ops.filter((o) => o.op !== 'ins').map((o) => o.text).join('')
  const after = ops.filter((o) => o.op !== 'del').map((o) => o.text).join('')
  check(`wordDiff rebuilds both sides: ${JSON.stringify([a, b])}`, before === a && after === b)
  check('wordDiff has no empty pieces', ops.every((o) => o.text))
}
{
  const ops = wordDiff('We show the colour of the model.', 'We show the color of a model.')
  check(`wordDiff is by word: ${JSON.stringify(ops)}`, JSON.stringify(ops) === JSON.stringify([
    { op: 'eq', text: 'We show the ' }, { op: 'del', text: 'colour' }, { op: 'ins', text: 'color' }, { op: 'eq', text: ' of ' },
    { op: 'del', text: 'the' }, { op: 'ins', text: 'a' }, { op: 'eq', text: ' model.' }]))
}

// ---- hunks ----
const invariants = (what, shared, hunks, view) => {
  check(`${what}: view is shared text with the hunks applied`, core.compose(shared, hunks) === view)
  for (let i = 0; i < hunks.length; i++) {
    const h = hunks[i]
    check(`${what}: hunk in bounds`, h.a >= 0 && h.a <= h.b && h.b <= shared.length)
    check(`${what}: hunks apart and in order`, i === 0 || h.a > hunks[i - 1].b)
    check(`${what}: hunk changes something`, shared.slice(h.a, h.b) !== h.text)
  }
}

const ME = 'tab-1'
for (let run = 0; run < 300; run++) {
  const doc = new Y.Doc({ gc: false })
  const files = doc.getMap('files'), review = doc.getMap('review')
  const yt = new Y.Text()
  files.set('main.tex', yt)
  yt.insert(0, words(20 + rnd(80)))
  // someone else's copy, for edits arriving over the network
  const other = new Y.Doc({ gc: false })
  Y.applyUpdate(other, Y.encodeStateAsUpdate(doc))
  const otherText = other.getMap('files').get('main.tex')
  const sync = () => { Y.applyUpdate(doc, Y.encodeStateAsUpdate(other, Y.encodeStateVector(doc))); Y.applyUpdate(other, Y.encodeStateAsUpdate(doc, Y.encodeStateVector(other))) }

  let shared = yt.toString()
  let hunks = []
  let view = shared
  const gone = new Set()
  const save = () => {
    hunks = core.saveHunks(doc, review, yt, hunks, gone, { file: 'main.tex', by: 'Me', me: ME, origin: 'test' })
    core.anchor(yt, hunks)
    gone.clear()
    const loaded = core.loadHunks(doc, review, yt, 'main.tex', ME).hunks
    check(`run ${run}: saved hunks load back the same`, JSON.stringify(loaded.map(({ id, a, b, text }) => [id, a, b, text])) === JSON.stringify(hunks.map(({ id, a, b, text }) => [id, a, b, text])))
    const open = [...review.values()].filter((m) => !m.get('resolved') && m.get('typed') === ME).length
    check(`run ${run}: one stored suggestion per hunk (${open} for ${hunks.length})`, open === hunks.length)
  }

  for (let step = 0; step < 60; step++) {
    const k = rnd(10)
    if (k < 7) { // typing in the view
      const f = snap(view, rnd(view.length + 1)), t = snap(view, Math.min(view.length, f + (rnd(3) ? 0 : rnd(12))))
      const s = rnd(5) ? piece() : ''
      const res = core.edit(shared, hunks, view, f, t, s)
      res.gone.forEach((id) => gone.add(id))
      hunks = res.hunks
      core.anchor(yt, hunks)
      view = view.slice(0, f) + s + view.slice(t)
      invariants(`run ${run} step ${step} edit`, shared, hunks, view)
    } else if (k < 8) save()
    else { // someone else edits the shared text (anchors are as the editor leaves them)
      const ot = otherText.toString(), at = snap(ot, rnd(ot.length + 1))
      if (rnd(2)) otherText.insert(at, piece() || 'z')
      else otherText.delete(at, snap(ot, Math.min(ot.length, at + 1 + rnd(8))) - at)
      sync()
      shared = yt.toString()
      const res = core.refresh(doc, yt, hunks)
      res.gone.forEach((id) => gone.add(id))
      // a remote edit can leave a hunk that no longer differs: tidy as the editor does
      hunks = res.hunks.filter((h) => shared.slice(h.a, h.b) !== h.text || (h.id && gone.add(h.id) && false))
      core.anchor(yt, hunks)
      view = core.compose(shared, hunks)
      invariants(`run ${run} step ${step} remote`, shared, hunks, view)
    }
  }
  save()
  // accepting every suggestion gives what was typed
  doc.transact(() => {
    for (const h of [...hunks].reverse()) core.applyChange(yt, h.a, shared.slice(h.a, h.b), h.text)
  })
  check(`run ${run}: accepting everything gives the typed text`, yt.toString() === view)
}

// mapping between the view and the shared text
{
  const shared = 'one two three four five'
  const hunks = [{ id: null, a: 4, b: 7, text: 'TWO2' }, { id: null, a: 14, b: 18, text: '' }]
  const view = core.compose(shared, hunks)
  check(`compose: ${view}`, view === 'one TWO2 three  five')
  check('toView before a hunk', core.toView(hunks, 2) === 2)
  check('toView inside a hunk is its start', core.toView(hunks, 5) === 4)
  check('toView after a hunk', core.toView(hunks, 8) === 9)
  check('toShared inside new text', core.toShared(hunks, 6, -1) === 4 && core.toShared(hunks, 6, 1) === 7)
  check('toShared after both', core.toShared(hunks, view.length) === shared.length)
}
// readable hunks
{
  const shared = 'This is good. Next line.'
  const n = (a, b, text) => { const h = core.normalize(shared, a, b, text, null); return h && [shared.slice(h.a, h.b), h.text] }
  check(`insertion takes the word before: ${n(7, 7, ' very')}`, JSON.stringify(n(7, 7, ' very')) === '["is","is very"]')
  check(`a change inside a word takes the word: ${n(9, 10, 'oo')}`, JSON.stringify(n(9, 10, 'oo')) === '["good","goood"]')
  check(`removing a space takes both words: ${n(4, 5, '')}`, JSON.stringify(n(4, 5, '')) === '["This is","Thisis"]')
  check('no change is no hunk', n(5, 7, 'is') === null)
}

// others' edits inside a suggestion
{
  const rb = core.rebase
  check(`a change to a word the suggestion keeps is carried in: ${rb('the red car is fast', 'the red cat is fast', 'the blue car is fast')}`,
    rb('the red car is fast', 'the red cat is fast', 'the blue car is fast') === 'the blue cat is fast')
  check('a change to a word the suggestion changes is a conflict', rb('the colour', 'the colXXour', 'the color') === null)
  check('an insertion between kept words is carried in', rb('one two three', 'one two and a half three', 'ONE two three') === 'ONE two and a half three')
}
// the reviewer's cases, through the same steps as the editor
{
  const doc = new Y.Doc({ gc: false })
  const files = doc.getMap('files'), review = doc.getMap('review')
  const yt = new Y.Text()
  files.set('main.tex', yt)
  yt.insert(0, 'alpha beta gamma delta')
  let shared = yt.toString(), hunks = []
  const type = (f, t, s) => { const res = core.edit(shared, hunks, core.compose(shared, hunks), f, t, s); hunks = res.hunks; core.anchor(yt, hunks); return res }
  const remote = (fn) => { fn(); shared = yt.toString(); hunks = core.refresh(doc, yt, hunks).hunks; core.anchor(yt, hunks) }
  type(0, 5, 'ALPHA'); type(11, 16, 'GAMMA')
  check(`two suggestions: ${core.compose(shared, hunks)}`, core.compose(shared, hunks) === 'ALPHA beta GAMMA delta' && hunks.length === 2)
  remote(() => yt.delete(5, 6)) // ' beta ' goes: the two touch and join
  remote(() => yt.insert(yt.length, ' more')) // and a joined one still has its place afterwards
  check(`joined suggestions survive later edits: ${core.compose(shared, hunks)}`, core.compose(shared, hunks) === 'ALPHAGAMMA delta more' && hunks.length === 1)

  yt.delete(0, yt.length); yt.insert(0, 'the colour of it')
  shared = yt.toString(); hunks = []
  type(4, 10, 'color')
  remote(() => yt.insert(7, 'XX')) // inside the very word being changed
  check('a collaborator editing the changed word marks a conflict', hunks.length === 1 && hunks[0].conflict === true)
  yt.delete(0, yt.length); yt.insert(0, 'the colour of the big red car')
  shared = yt.toString(); hunks = []
  type(4, 10, 'color'); type(core.compose(shared, hunks).length - 3, core.compose(shared, hunks).length, 'van') // one hunk? no: two apart
  remote(() => yt.insert(yt.toString().indexOf('big'), 'very ')) // between them, outside both
  check(`edits outside suggestions show: ${core.compose(shared, hunks)}`, core.compose(shared, hunks) === 'the color of the very big red van')
}
// someone typing right against a word the suggestion took in
{
  const doc = new Y.Doc({ gc: false })
  const yt = new Y.Text()
  doc.getMap('files').set('main.tex', yt)
  yt.insert(0, 'This is good.')
  let shared = yt.toString(), hunks = []
  const type = (f, t, s) => { hunks = core.edit(shared, hunks, core.compose(shared, hunks), f, t, s).hunks; core.anchor(yt, hunks) }
  const remote = (fn) => { fn(); shared = yt.toString(); hunks = core.refresh(doc, yt, hunks).hunks; core.anchor(yt, hunks) }
  type(7, 7, ' very') // "is" → "is very"
  remote(() => yt.insert(7, "n't")) // carries the word on
  check(`a word carried on is part of the suggestion: ${core.compose(shared, hunks)}`, core.compose(shared, hunks) === "This isn't very good.")
  remote(() => yt.insert(yt.toString().indexOf("isn't") + 5, ' really')) // starts with a space: beside it
  check(`text beside a suggestion stays outside it: ${core.compose(shared, hunks)}`, core.compose(shared, hunks) === "This isn't very really good." && hunks[0].text === "isn't very")
  remote(() => yt.insert(0, 'So ')) // and before it, nothing changes
  check(`text before stays outside: ${core.compose(shared, hunks)}`, core.compose(shared, hunks) === "So This isn't very really good.")
}

// stored suggestions pushed together are joined on loading, and the extra one goes
{
  const doc = new Y.Doc({ gc: false })
  const files = doc.getMap('files'), review = doc.getMap('review')
  const yt = new Y.Text()
  files.set('main.tex', yt)
  yt.insert(0, 'alpha beta gamma delta')
  let hunks = core.edit(yt.toString(), [], yt.toString(), 0, 5, 'ALPHA').hunks
  hunks = core.edit(yt.toString(), hunks, core.compose(yt.toString(), hunks), 11, 16, 'GAMMA').hunks
  hunks = core.saveHunks(doc, review, yt, hunks, [], { file: 'main.tex', by: 'Me', me: 'tab', origin: 'test' })
  yt.delete(5, 6)
  const loaded = core.loadHunks(doc, review, yt, 'main.tex', 'tab')
  check(`loading joins them: ${JSON.stringify(loaded.hunks.map((h) => h.text))} gone ${loaded.gone.length}`, loaded.hunks.length === 1 && loaded.hunks[0].text === 'ALPHAGAMMA' && loaded.gone.length === 1)
}

if (bad.length) { console.error('suggest: FAILED\n' + bad.join('\n')); process.exit(1) }
console.log('suggest: all checks passed')
