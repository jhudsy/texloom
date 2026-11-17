// Model-based check of the browser's folder sync (src/foldersync.ts), as scripts/check_folder_model.py
// does for the desktop's: fast-check runs the real startFolderSync in two browsers (A and B, each
// syncing a folder) with a collaborator (C) through random sequences of what can happen, and checks
// after every step that nothing was lost or undone.
//
// One file, main.tex, in parts, each written by one person only: the editor on A's folder, the
// editor on B's folder (Windows line endings), typing into A's project, and C. What each part must
// hold is therefore known exactly: its writer's last version.
//
// Steps: an editor saves; typing; changes delivered between any two of A, B and C; time passing (the
// sync's poll, its waits for a save to settle, its delayed writes all run on this fake clock); a
// folder's writes refused or allowed (Windows, the file open elsewhere); an older copy put back after
// the sync wrote the newest (a stale editor buffer); the page reloaded (memory gone, browser storage
// kept). After every step: each editor's part on disk is what it saved; typed parts are kept. At the
// end, with writes allowed, everything delivered and time passed: all copies and both folders agree,
// every part as its writer left it, and B's file has Windows line endings again.
//
// The folder (File System Access handles), browser storage, timers and clock are in-memory fakes, so
// a failing sequence replays exactly (CHECK_SEED=…), shrunk to the shortest one that shows it.
import path from 'node:path'
import { createHash } from 'node:crypto'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { build } from 'vite'
import * as Y from 'yjs'
import fc from 'fast-check'
import { readFileSync } from 'node:fs'

const web = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const out = path.join(web, 'node_modules/.cache/check-folder-model')
globalThis.localStorage ??= { getItem: () => null, setItem() {}, removeItem() {} }
const load = async (entry) => {
  const name = path.basename(entry, '.ts') + '.mjs'
  await build({ configFile: false, root: web, logLevel: 'warn', build: { ssr: entry, outDir: out, emptyOutDir: false, minify: false, rollupOptions: { output: { entryFileNames: name } } } })
  return import(pathToFileURL(path.join(out, name)).href)
}
const { startFolderSync, rememberFolder } = await load('src/foldersync.ts')
const { applyText } = await load('src/diff.ts')
const { keepLF } = await load('src/lineends.ts')

// ---- the fake world: clock and timers, browser storage, folders --------------------------------
const clock = { now: 1_700_000_000_000, timers: new Map(), next: 1 }
Date.now = () => clock.now
globalThis.setTimeout = (fn, ms = 0) => { const id = clock.next++; clock.timers.set(id, { at: clock.now + Math.max(0, ms), fn }); return id }
globalThis.clearTimeout = (id) => { clock.timers.delete(id) }
globalThis.setInterval = (fn, ms) => { const id = clock.next++; const tick = () => { clock.timers.set(id, { at: clock.now + ms, fn: tick }); fn() }; clock.timers.set(id, { at: clock.now + ms, fn: tick }); return id }
globalThis.clearInterval = globalThis.clearTimeout
globalThis.document = { visibilityState: 'visible' }
console.warn = () => {} // (the sync's notes about stale copies: expected here)
// sha-256 without leaving the microtask queue, so a step's work finishes before the next
Object.defineProperty(globalThis.crypto.subtle, 'digest', { value: async (_alg, data) => { const b = createHash('sha256').update(new Uint8Array(data.buffer ?? data, data.byteOffset ?? 0, data.byteLength)).digest(); return b.buffer.slice(b.byteOffset, b.byteOffset + b.byteLength) } })

const flush = async () => { for (let i = 0; i < 40; i++) await new Promise((r) => setImmediate(r)) }
async function pass(ms) { // time passes: due timers fire in order, each one's work finishing first
  const end = clock.now + ms
  for (;;) {
    let first = null
    for (const [id, t] of clock.timers) if (t.at <= end && (!first || t.at < first[1].at)) first = [id, t]
    if (!first) break
    clock.now = Math.max(clock.now, first[1].at)
    clock.timers.delete(first[0])
    first[1].fn()
    await flush()
  }
  clock.now = end
  await flush()
}

// IndexedDB, as much as foldersync's kv() uses (one object store, get/put/delete)
const stores = new Map()
const request = (fn) => { const q = {}; queueMicrotask(() => { try { q.result = fn(); q.onsuccess?.() } catch (e) { q.error = e; q.onerror?.() } }); return q }
globalThis.indexedDB = { open: () => request(() => ({
  transaction: () => ({ objectStore: () => ({
    get: (k) => request(() => stores.get(k)), put: (v, k) => request(() => { stores.set(k, v instanceof FakeFolder ? v : structuredClone(v)) }), delete: (k) => request(() => { stores.delete(k) }),
  }) }),
})) }

const notFound = () => Object.assign(new Error('not found'), { name: 'NotFoundError' })
class FakeFile { // a File handle's getFile()
  constructor(name, data, m) { this.name = name; this.data = data; this.size = data.length; this.lastModified = m }
  async arrayBuffer() { return this.data.slice().buffer }
  async text() { return new TextDecoder().decode(this.data) }
}
class FakeFolder { // FileSystemDirectoryHandle over a Map: name -> { data, m } or FakeFolder
  constructor(name) { this.kind = 'directory'; this.name = name; this.items = new Map(); this.refusing = false; this.log = [] }
  async *entries() { for (const [n, v] of this.items) yield [n, v instanceof FakeFolder ? v : this.fileHandle(n)] }
  async *keys() { for (const n of this.items.keys()) yield n }
  async getDirectoryHandle(n, o = {}) { const v = this.items.get(n); if (v instanceof FakeFolder) return v; if (v || !o.create) throw notFound(); const d = new FakeFolder(n); this.items.set(n, d); return d }
  async getFileHandle(n, o = {}) { const v = this.items.get(n); if (v instanceof FakeFolder) throw notFound(); if (!v) { if (!o.create) throw notFound(); this.items.set(n, { data: new Uint8Array(), m: clock.now }) } return this.fileHandle(n) }
  async removeEntry(n) { if (!this.items.delete(n)) throw notFound() }
  async isSameEntry(o) { return o === this }
  fileHandle(n) {
    const folder = this
    return {
      kind: 'file', name: n,
      async getFile() { const v = folder.items.get(n); if (!v) throw notFound(); return new FakeFile(n, v.data, v.m) },
      async createWritable() {
        if (folder.refusing) throw Object.assign(new Error('The file is open in another program (model)'), { name: 'NoModificationAllowedError' })
        let buf = new Uint8Array()
        return { async write(d) { buf = new Uint8Array(d.buffer ? d.buffer.slice(d.byteOffset, d.byteOffset + d.byteLength) : d) }, async close() { folder.items.set(n, { data: buf, m: clock.now }); folder.log.push(buf) } }
      },
    }
  }
  // the test's own access, as an editor or another program
  read(n) { const v = this.items.get(n); return v ? new TextDecoder().decode(v.data) : null }
  write(n, text) { clock.now += 1000; this.items.set(n, { data: new TextEncoder().encode(text), m: clock.now }) }
}

// ---- the file's parts ----------------------------------------------------------------------------
const F = 'main.tex', ZONES = ['edA', 'edB', 'typA', 'C']
const sep = (z) => `%% ---- ${z} ----`
const render = (parts) => ['\\documentclass{article}', ...ZONES.flatMap((z) => [sep(z), ...parts[z]]), '%% end'].join('\n') + '\n'
/** A text's parts; `raw`: lines exactly as they are (other people's "\r" included), for editing
 *  one part without touching the rest, as typing does. */
function parse(text, raw = false) {
  if (text == null) return null
  const lines = (raw ? text : text.replace(/\r\n/g, '\n')).split('\n')
  if (lines[lines.length - 1] === '') lines.pop()
  const bare = lines.map((l) => l.replace(/\r$/, ''))
  const at = [...ZONES.map((z) => bare.indexOf(sep(z))), bare.indexOf('%% end')]
  if (at.includes(-1) || at.some((x, i) => i && x < at[i - 1]) || bare[0] !== '\\documentclass{article}') return null
  const parts = Object.fromEntries(ZONES.map((z, i) => [z, lines.slice(at[i] + 1, at[i + 1])]))
  if (raw) parts.head = [lines[0], ...ZONES.map((z, i) => lines[at[i]]), lines[at[ZONES.length]]] // the fixed lines, as they are
  return parts
}
/** The text again, with one part replaced (the other lines as they were). */
const renderRaw = (parts) => [parts.head[0], ...ZONES.flatMap((z, i) => [parts.head[i + 1], ...parts[z]]), parts.head[ZONES.length + 1]].join('\n') + '\n'
const bare = (lines) => lines.map((l) => l.replace(/\r$/, ''))
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b)

function replica(doc, who) { // what startFolderSync (and keepLF) use of a Replica
  const blobs = new Map()
  return {
    isRemote: (o) => o === 'remote', isLocal: (o) => o !== 'remote', deletedAt: null,
    doc, files: doc.getMap('files'), bin: doc.getMap('bin'), dirs: doc.getMap('dirs'), meta: doc.getMap('meta'),
    // the project id names each browser's stored state: one per browser here, as each has its own storage
    keys: { project: `p${who}`, writable: true, blobId: async (d) => createHash('sha256').update(d).digest('hex').slice(0, 32) },
    putBlob: async (d) => { const id = createHash('sha256').update(d).digest('hex').slice(0, 32); blobs.set(id, d); return id },
    getBlob: async (id) => blobs.get(id),
  }
}

class World {
  constructor() {
    stores.clear(); clock.timers.clear()
    this.n = 0
    this.want = Object.fromEntries(ZONES.map((z) => [z, [`${z} start`]]))
    this.docs = { A: new Y.Doc(), B: new Y.Doc(), C: new Y.Doc() }
    this.folders = { A: new FakeFolder('A'), B: new FakeFolder('B') }
    this.syncs = {}
    this.editedB = false
  }
  async start(who) {
    const r = replica(this.docs[who], who)
    if (!this.lf?.[who]) { (this.lf ??= {})[who] = true; keepLF(r) } // as the web app does for an open project
    this.syncs[who] = await startFolderSync(r, this.folders[who])
    await rememberFolder(`p${who}`, this.folders[who]) // as main.ts does once a folder is linked
  }
  async setup() {
    this.folders.A.write(F, render(this.want))
    await this.start('A'); await pass(1000) // A takes in its file
    await this.start('B')
    await this.deliver('A', 'B'); await this.deliver('A', 'C')
    await pass(5000) // B writes it out
    if (this.folders.B.read(F) == null || this.text('C') !== render(this.want)) throw new Error('setup: the file didn\'t reach B and C')
  }
  token(z) { this.n++; return this.n % 3 === 0 ? `${z} ${this.n} é😀` : `${z} ${this.n}` }
  changed(z, lines, op, at) {
    lines = [...lines]
    if (op === 0 || !lines.length) lines.splice(at % (lines.length + 1), 0, this.token(z))
    else if (op === 1) lines.splice(at % lines.length, 1)
    else { const i = at % lines.length; lines[i] += ' ✎' }
    return lines
  }
  async deliver(src, dst) {
    if (src === dst || (src !== 'C' && !this.syncs[src]) || (dst !== 'C' && !this.syncs[dst])) return
    const u = Y.encodeStateAsUpdate(this.docs[src], Y.encodeStateVector(this.docs[dst]))
    Y.applyUpdate(this.docs[dst], u, 'remote')
    await flush()
  }
  text(who) { return this.docs[who].getMap('files').get(F)?.toString() ?? '' }
}

// ---- the steps ----------------------------------------------------------------------------------
class EditorSaves {
  constructor(who, op, at) { Object.assign(this, { who, op, at }) }
  run = async (w) => { await this.act(w); invariants(w) }
  check = () => true
  async act(w) {
    const f = w.folders[this.who], z = `ed${this.who}`, parts = parse(f.read(F))
    if (!parts) throw new Error(`${this.who}'s file is damaged: ${JSON.stringify(f.read(F))}`)
    parts[z] = w.want[z] = w.changed(z, parts[z], this.op, this.at)
    const text = render(parts)
    f.write(F, this.who === 'B' ? text.replace(/\n/g, '\r\n') : text)
    if (this.who === 'B') w.editedB = true
    await flush()
  }
  toString() { return `editor saves on ${this.who} (${['insert', 'delete', 'change'][this.op]} @${this.at})` }
}
class TypedInA { // (the web app on A: only while its page is open)
  constructor(op, at) { Object.assign(this, { op, at }) }
  run = async (w) => { await this.act(w); invariants(w) }
  check = (w) => !!w.syncs.A
  async act(w) {
    const t = w.docs.A.getMap('files').get(F), cur = t.toString(), parts = parse(cur, true)
    parts.typA = w.want.typA = w.changed('typA', bare(parts.typA), this.op, this.at)
    w.docs.A.transact(() => applyText(t, cur, renderRaw(parts)))
    await flush()
  }
  toString() { return `typed in A (${this.op} @${this.at})` }
}
class CollaboratorTypes {
  constructor(op, at) { Object.assign(this, { op, at }) }
  run = async (w) => { await this.act(w); invariants(w) }
  check = () => true
  async act(w) {
    const t = w.docs.C.getMap('files').get(F), cur = t.toString(), parts = parse(cur, true)
    parts.C = w.want.C = w.changed('C', bare(parts.C), this.op, this.at)
    w.docs.C.transact(() => applyText(t, cur, renderRaw(parts)))
  }
  toString() { return `collaborator types (${this.op} @${this.at})` }
}
class CollaboratorPastesWindowsText { // C's older app puts lines ending "\r\n" into the project
  constructor(op, at) { Object.assign(this, { op, at }) }
  run = async (w) => { await this.act(w); invariants(w) }
  check = () => true
  async act(w) {
    const t = w.docs.C.getMap('files').get(F), cur = t.toString(), parts = parse(cur, true)
    parts.C = w.want.C = w.changed('C', bare(parts.C), this.op, this.at)
    w.docs.C.transact(() => applyText(t, cur, renderRaw({ ...parts, C: parts.C.map((l) => l + '\r') })))
  }
  toString() { return `collaborator pastes Windows text (${this.op} @${this.at})` }
}
class Deliver {
  constructor(src, dst) { Object.assign(this, { src, dst }) }
  run = async (w) => { await this.act(w); invariants(w) }
  check = () => true
  async act(w) { await w.deliver(this.src, this.dst) }
  toString() { return `deliver ${this.src}→${this.dst}` }
}
class TimePasses {
  constructor(ms) { this.ms = ms }
  run = async (w) => { await this.act(w); invariants(w) }
  check = () => true
  async act() { await pass(this.ms) }
  toString() { return `${this.ms} ms pass` }
}
class Refuse {
  constructor(who, on) { Object.assign(this, { who, on }) }
  run = async (w) => { await this.act(w); invariants(w) }
  check = () => true
  async act(w) { w.folders[this.who].refusing = this.on }
  toString() { return `${this.who}'s writes ${this.on ? 'refused' : 'allowed'}` }
}
class OlderCopy { // after the sync wrote the newest version, an older one it wrote comes back
  constructor(who, pick) { Object.assign(this, { who, pick }) }
  run = async (w) => { await this.act(w); invariants(w) }
  check = (w) => !!w.syncs[this.who] && !w.folders[this.who].refusing
  async act(w) {
    const f = w.folders[this.who], log = f.log, cur = f.items.get(F)?.data
    if (log.length < 2 || !cur || !same([...log[log.length - 1]], [...cur])) return // the newest isn't the sync's own write
    const older = log.slice(0, -1).filter((d) => !same([...d], [...cur]))
    if (!older.length) return
    f.write(F, new TextDecoder().decode(older[this.pick % older.length]))
    await pass(3000) // the sync looks, and must put the project's text back
  }
  toString() { return `older copy comes back on ${this.who} (#${this.pick})` }
}
class CloseOrOpen { // the page is closed (memory gone, browser storage kept), or opened again
  constructor(who) { this.who = who }
  run = async (w) => { await this.act(w); invariants(w) }
  check = () => true
  async act(w) {
    if (w.syncs[this.who]) { w.syncs[this.who].stop(); delete w.syncs[this.who] }
    else await w.start(this.who)
    await flush()
  }
  toString() { return `${this.who}'s page ${'closes or opens'}` }
}

function invariants(w) {
  for (const who of ['A', 'B']) {
    const parts = parse(w.folders[who].read(F)), z = `ed${who}`
    if (!parts) throw new Error(`${who}'s file is damaged: ${JSON.stringify(w.folders[who].read(F))}`)
    if (!same(parts[z], w.want[z])) throw new Error(`${who}'s editor saved ${JSON.stringify(w.want[z])}, the file now has ${JSON.stringify(parts[z])}`)
  }
  const a = parse(w.text('A')), c = parse(w.text('C'))
  if (w.syncs.A && (!a || !same(a.typA, w.want.typA))) throw new Error(`typed in A: ${JSON.stringify(w.want.typA)}, A's project: ${JSON.stringify(a?.typA)}`)
  if (!c || !same(c.C, w.want.C)) throw new Error(`C typed ${JSON.stringify(w.want.C)}, C's copy: ${JSON.stringify(c?.C)}`)
}

async function converges(w) {
  w.folders.A.refusing = w.folders.B.refusing = false
  for (const x of ['A', 'B']) if (!w.syncs[x]) await w.start(x)
  for (let i = 0; i < 4; i++) {
    for (const s of 'ABC') for (const d of 'ABC') await w.deliver(s, d)
    await pass(6000)
  }
  const texts = { A: w.text('A'), B: w.text('B'), C: w.text('C') }
  if (new Set(Object.values(texts)).size !== 1) throw new Error(`the copies differ: ${JSON.stringify(texts)}`)
  if (!same(parse(texts.A), w.want)) throw new Error(`want ${JSON.stringify(w.want)}, have ${JSON.stringify(parse(texts.A))} (A: ${w.syncs.A?.status()}; B: ${w.syncs.B?.status()}; A's file: ${JSON.stringify(w.folders.A.read(F)?.slice(0, 60))})`)
  if (w.folders.A.read(F) !== texts.A) throw new Error(`A's file isn't the project's: ${JSON.stringify(w.folders.A.read(F))}`)
  const b = w.folders.B.read(F)
  if (b?.replace(/\r\n/g, '\n') !== texts.A) throw new Error(`B's file isn't the project's: ${JSON.stringify(b)}`)
  if (w.editedB && b.split('\r\n').length !== texts.A.split('\n').length) throw new Error(`B's file lost its Windows line endings: ${JSON.stringify(b)}`)
}

if (process.env.CHECK_TRACE) { // one browser, one file: what the sync does, step by step
  const w = new World()
  w.folders.A.write(F, 'hello\n')
  const origSet = globalThis.setTimeout
  await w.start('A')
  console.log('after start: doc =', JSON.stringify(w.text('A')), 'timers =', [...clock.timers.values()].map((t) => t.at - clock.now), 'status =', w.syncs.A.status())
  for (const ms of [300, 300, 2000, 2000]) { await pass(ms); console.log(`+${ms}: doc =`, JSON.stringify(w.text('A')), 'timers =', [...clock.timers.values()].map((t) => t.at - clock.now), 'status =', w.syncs.A.status()) }
  void origSet; process.exit(0)
}
const who = fc.constantFrom('A', 'B'), op = fc.integer({ min: 0, max: 2 }), at = fc.nat({ max: 50 })
const commands = fc.commands([
  fc.tuple(who, op, at).map(([a, b, c]) => new EditorSaves(a, b, c)),
  fc.tuple(op, at).map(([a, b]) => new TypedInA(a, b)),
  fc.tuple(op, at).map(([a, b]) => new CollaboratorTypes(a, b)),
  fc.tuple(op, at).map(([a, b]) => new CollaboratorPastesWindowsText(a, b)),
  fc.tuple(fc.constantFrom('A', 'B', 'C'), fc.constantFrom('A', 'B', 'C')).map(([a, b]) => new Deliver(a, b)),
  fc.constantFrom(100, 300, 1000, 2500, 6000).map((ms) => new TimePasses(ms)),
  fc.tuple(who, fc.boolean()).map(([a, b]) => new Refuse(a, b)),
  fc.tuple(who, at).map(([a, b]) => new OlderCopy(a, b)),
  who.map((a) => new CloseOrOpen(a)),
], { maxCommands: 40 })

// ---- the known cases first (scripts/folder-cases.json): every failure found so far, as its steps --
const STEP = {
  save: (who, op, at) => new EditorSaves(who, op, at), type: (op, at) => new TypedInA(op, at),
  collab: (op, at) => new CollaboratorTypes(op, at), collab_crlf: (op, at) => new CollaboratorPastesWindowsText(op, at),
  deliver: (src, dst) => new Deliver(src, dst), sync: () => new TimePasses(3000), time: (ms) => new TimePasses(ms),
  refuse: (who, on) => new Refuse(who, on), older: (who, pick) => new OlderCopy(who, pick), toggle: (who) => new CloseOrOpen(who),
}
const known = JSON.parse(readFileSync(path.join(web, '..', 'scripts', 'folder-cases.json'), 'utf8')).cases
if (process.env.CHECK_STEPS) { // replay one sequence, showing A's file and project after each step
  const w = new World(); await w.setup()
  const show = (what) => console.log(`${what}\n   file:    ${JSON.stringify(w.folders.A.read(F)?.split('%% ---- C ----')[1])}\n   project: ${JSON.stringify(w.text('A').split('%% ---- C ----')[1])}\n   C:       ${JSON.stringify(w.text('C').split('%% ---- C ----')[1])}`)
  for (const [kind, ...args] of JSON.parse(process.env.CHECK_STEPS)) { const st = STEP[kind](...args); if (st.check(w)) await st.act(w); show(st.toString()) }
  try { await converges(w); console.log('converged') } catch (e) { show('end'); console.log(e.message.slice(0, 300)) }
  process.exit(0)
}
const failed = []
for (const c of known) {
  try {
    const w = new World()
    await w.setup()
    for (const [kind, ...args] of c.steps) { const s = STEP[kind](...args); if (s.check(w)) await s.run(w) }
    await converges(w)
    for (const s of Object.values(w.syncs)) s.stop()
  } catch (e) { failed.push(`${c.name}: ${String(e.message).slice(0, 400)}`) }
}
if (failed.length) { console.error('folder model (browser): FAILED known cases (scripts/folder-cases.json)\n  ' + failed.join('\n  ')); process.exit(1) }

const seed = Number(process.env.CHECK_SEED) || Date.UTC(2026, 0, 1) % 2 ** 31 + Math.floor(Math.random() * 1e6)
const runs = Number(process.env.CHECK_RUNS) || 150
try {
  await fc.assert(fc.asyncProperty(commands, async (cmds) => {
    const w = new World()
    await w.setup()
    await fc.asyncModelRun(() => ({ model: w, real: w }), cmds)
    await converges(w)
    for (const s of Object.values(w.syncs)) s.stop()
  }), { seed, numRuns: runs })
  console.log(`folder model (browser): every known case and every step held (${runs} random runs of up to 40 steps, seed ${seed})`)
} catch (e) {
  console.error(`folder model (browser): FAILED (seed ${seed})\n${String(e.message).split('\n').slice(0, 30).join('\n')}\n${e.cause ? String(e.cause.stack ?? e.cause).slice(0, 1500) : ''}`)
  process.exit(1)
}
