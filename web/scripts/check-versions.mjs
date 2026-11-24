// Checks web/src/versions.ts: every saved version must open to exactly what the project
// held when it was saved, however it was stored, and storing them must stay small.
import * as Y from 'yjs'
import { load, report } from './lib.mjs'

const [{ saveVersion, docAt, nextLink, versionLabel, nameVersion, thinned }] = await load('src/versions.ts')

const bad = []
const check = (what, ok) => { if (!ok) bad.push(what) }

{ // automatic versions thin out with age: all from the last day, the newest per hour for a week, per day before
  const H = 3600_000, D = 24 * H, now = Date.UTC(2026, 10, 16, 12)
  const autos = []
  for (let at = now - 40 * D; at <= now; at += 15 * 60_000) autos.push({ at, by: 'x', auto: true, link: String(at) }) // as often as autoSave saves
  const drop = thinned(autos, now), kept = autos.filter((v) => !drop.has(v))
  check('thin: everything from the last day kept', autos.filter((v) => now - v.at < D).every((v) => !drop.has(v)))
  const per = (vs, size) => { const m = new Map(); for (const v of vs) m.set(Math.floor(v.at / size), (m.get(Math.floor(v.at / size)) ?? 0) + 1); return m }
  check('thin: one per hour within the week', [...per(kept.filter((v) => now - v.at >= D && now - v.at < 7 * D), H).values()].every((n) => n === 1))
  check('thin: one per day before that', [...per(kept.filter((v) => now - v.at >= 7 * D), D).values()].every((n) => n === 1))
  const week = autos.filter((v) => now - v.at >= D && now - v.at < 7 * D), newestIn = new Map()
  for (const v of week) newestIn.set(Math.floor(v.at / H), Math.max(v.at, newestIn.get(Math.floor(v.at / H)) ?? 0))
  check('thin: the newest in each hour is the one kept', week.every((v) => drop.has(v) === (v.at !== newestIn.get(Math.floor(v.at / H)))))
  check('thin: at most 300', kept.length <= 300)
  check('thin: weeks of saving every quarter hour keep about a day, a week of hours and a day each before', Math.abs(kept.length - (96 + 6 * 24 + 33)) <= 3)
}
const replica = () => {
  const doc = new Y.Doc({ gc: false })
  return { doc, versions: doc.getArray('versions'), versionChain: doc.getArray('versionChain') }
}
let seed = 20261108
const rnd = (n) => { seed = (seed * 1664525 + 1013904223) >>> 0; return Math.floor(seed / 2 ** 32 * n) }
const WORDS = 'the of model we show that proof lemma result section data \\cite{a} $x$'.split(' ')
const words = (n) => { let s = ''; while (s.length < n) s += WORDS[rnd(WORDS.length)] + ' '; return s }
const text = (r, name = 'main.tex') => {
  const files = r.doc.getMap('files')
  if (!files.has(name)) files.set(name, new Y.Text())
  return files.get(name)
}
function edit(r, n = 60, target = 2000, name) {
  const t = text(r, name)
  r.doc.transact(() => t.insert(rnd(t.length + 1), words(n)))
  if (t.length > target) r.doc.transact(() => { const at = rnd(t.length - n); t.delete(at, n) })
}
const state = (d) => { const o = {}; d.getMap('files').forEach((t, p) => { o[p] = t.toString() }); return JSON.stringify(Object.entries(o).sort()) }
const opens = (r, v, want, what) => {
  try { check(`${what}: wrong content`, state(docAt(r, v)) === want) } catch (e) { bad.push(`${what}: ${e}`) }
}
const last = (r) => r.versions.get(r.versions.length - 1)

{ // every version opens to exactly what was saved, with several files and whole-file deletions
  const r = replica(), want = new Map()
  for (let i = 0; i < 200; i++) {
    for (let k = rnd(5) + 1; k--;) edit(r, 40, 1500, ['main.tex', 'intro.tex', 'refs.bib'][rnd(3)])
    if (i === 90) r.doc.getMap('files').delete('intro.tex')
    if (i % 7 === 0) r.doc.clientID = 1 + rnd(2 ** 30) // a new page load gets a new client id
    const label = i % 25 === 0 ? `named ${i}` : undefined
    const now = state(r.doc)
    saveVersion(r, 'someone', label, !label)
    want.set(last(r), now)
  }
  check('all listed', r.versions.length === 200)
  want.forEach((s, v) => opens(r, v, s, 'history'))
  const links = r.versionChain.toArray()
  check('first link is whole', links[0].base === null)
  // every 64th link holds the whole snapshot again, so rebuilding one stays bounded
  check('later links build on the chain, but not too far back', links.every((l, i) => (i % 64 === 0) === (l.base === null)))
  // a late joiner gets everything in one update
  const late = replica(); Y.applyUpdate(late.doc, Y.encodeStateAsUpdate(r.doc))
  want.forEach((s, v) => opens(late, late.versions.toArray().find((x) => x.link === v.link), s, 'late joiner'))
  // only what's new goes in a link, and repeating a save adds next to nothing
  const before = last(r); saveVersion(r, 'someone')
  check('unchanged save is tiny', r.versionChain.get(r.versionChain.length - 1).snap.length < 80 && !!before)
}

{ // the limit on automatic saves unlists old ones; what's left still opens; named ones stay
  const r = replica(), want = new Map()
  for (let i = 0; i < 340; i++) {
    edit(r, 30, 800)
    const now = state(r.doc)
    saveVersion(r, `someone ${i}`, i === 3 ? 'keep me' : undefined, i !== 3) // names go by time and author
    want.set(last(r).link, now)
    if (i === 10) nameVersion(r, last(r), 'named later')
  }
  const listed = r.versions.toArray()
  check(`limit: ${listed.length} listed`, listed.length === 302)
  check('named survive', listed.some((v) => v.label === 'keep me') && listed.some((v) => versionLabel(r, v) === 'named later'))
  for (const v of listed) opens(r, v, want.get(v.link), 'after the limit')
  check('links are all kept', r.versionChain.length === 340)
}

{ // two people saving while apart: both forks open everywhere, and the next save still chains
  const a = replica(), b = replica()
  const sync = () => { for (const [x, y] of [[a, b], [b, a]]) Y.applyUpdate(y.doc, Y.encodeStateAsUpdate(x.doc, Y.encodeStateVector(y.doc))) }
  edit(a); saveVersion(a, 'a'); sync()
  const want = new Map()
  for (let round = 0; round < 15; round++) {
    for (const r of [a, b]) {
      for (let k = 3; k--;) edit(r, 40, 600)
      const now = state(r.doc); saveVersion(r, r === a ? 'a' : 'b'); want.set(last(r).link, now)
    }
    if (round % 3 === 2) sync()
  }
  sync()
  check('same links on both', a.versionChain.length === b.versionChain.length)
  for (const r of [a, b]) for (const v of r.versions.toArray()) if (want.has(v.link)) opens(r, v, want.get(v.link), 'forked')
  const now = state(a.doc); edit(a); const after = state(a.doc); saveVersion(a, 'a')
  check('chains after a fork', a.versionChain.get(a.versionChain.length - 1).base !== null && now !== after)
  opens(a, last(a), after, 'after a fork')
}

{ // a chain that can't reproduce the present falls back to a whole snapshot
  const fresh = () => { const r = replica(); edit(r); edit(r); saveVersion(r, 'x'); return r }
  const whole = (r) => { edit(r); const s = state(r.doc); saveVersion(r, 'x'); check('whole copy', r.versionChain.get(r.versionChain.length - 1).base === null); opens(r, last(r), s, 'whole copy') }
  let r = fresh(); r.versionChain.push([{ id: 'broken', base: 'nowhere', snap: new Uint8Array() }]); whole(r)
  r = fresh(); r.versionChain.push([{ id: 'loop', base: 'loop', snap: Y.encodeSnapshot(Y.snapshot(r.doc)) }]); whole(r)
  r = fresh(); r.versionChain.push([{ id: 'junk', base: null, snap: new Uint8Array([200, 200, 200]) }]); whole(r)
  r = fresh(); r.versionChain.push(['not a link']); whole(r)
  // a link that knows of a deletion this device hasn't seen
  r = fresh()
  const ds = Y.createDeleteSet(); ds.clients.set(r.doc.clientID, [{ clock: 0, len: 3 }])
  const tip = r.versionChain.get(r.versionChain.length - 1)
  r.versionChain.push([{ id: 'ahead', base: tip.id, snap: Y.encodeSnapshot(Y.createSnapshot(ds, new Map())) }])
  check('unseen deletion is not trusted', nextLink(r.versionChain, Y.snapshot(r.doc)).base === null)
  // a version whose chain is damaged is refused rather than shown wrongly
  r = fresh(); r.versions.push([{ at: 1, by: 'x', link: 'nowhere' }])
  let threw = false; try { docAt(r, last(r)) } catch { threw = true }
  check('damaged chain throws', threw)
}

// Size and speed at the scale of a thesis: a million characters typed, saving every 2,000.
function oldSave(r) { // a whole snapshot per version, for comparison
  const snap = Y.encodeSnapshot(Y.snapshot(r.doc))
  r.doc.transact(() => { r.versions.push([{ at: 1, by: 'someone', auto: true, snap }]); if (r.versions.length > 300) r.versions.delete(0, 1) })
}
const mb = (r) => (Y.encodeStateAsUpdate(r.doc).length / 1e6)
function scale(total, every, save) {
  seed = 42
  const r = replica(); let typed = 0, since = 0, saves = 0, worst = 0
  while (typed < total) {
    edit(r, 60, Math.min(150000, total / 3)); typed += 60; since += 60
    if (since >= every) {
      since = 0; if (++saves % 3 === 0) r.doc.clientID = 1 + rnd(2 ** 30)
      const t0 = performance.now(); save(r); worst = Math.max(worst, performance.now() - t0)
    }
  }
  return { r, saves, worst }
}
for (const [label, total, every] of [['paper', 150000, 1500], ['thesis', 1000000, 2000]]) {
  const o = scale(total, every, oldSave), n = scale(total, every, (r) => saveVersion(r, 'someone', undefined, true))
  const t0 = performance.now(); docAt(n.r, last(n.r)); const open = performance.now() - t0
  console.log(`${label}: ${n.saves} saves. Snapshot before ${mb(o.r).toFixed(2)} MB, now ${mb(n.r).toFixed(2)} MB. Slowest save ${o.worst.toFixed(0)} ms before, ${n.worst.toFixed(0)} ms now. Opening the newest version: ${open.toFixed(0)} ms`)
  check(`${label}: at most half the old size`, mb(n.r) < mb(o.r) / (label === 'thesis' ? 3 : 1.5))
  if (label === 'thesis') check('thesis: well under the 12 MB limit', mb(n.r) < 4)
}

report(`versions (${bad.length} failed, the first 20 here)`, bad.slice(0, 20), 'versions: all checks passed')
