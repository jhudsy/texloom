// Runs web/src/replica.ts through scripts/replica-vectors.json, the cases scripts/check_replica.py runs
// texloom/replica.py through: what a replica does with the server's messages (catching up,
// signatures, uploads, revocations, refusals, peers) must be the same in both apps.
//
// Each case gets a fresh replica, with a stand-in connection that records what it sends, a
// stand-in server for uploads and an in-memory stand-in for the browser's storage. Nothing
// touches the network.
//
//     npm run check-replica [words in a case's name]
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { build } from 'vite'

const web = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const out = path.join(web, 'node_modules/.cache/check-replica')
const SERVER = 'http://test'
Object.defineProperty(globalThis, 'localStorage', { value: { getItem: () => null, setItem() {}, removeItem() {} }, configurable: true }) // (not node's own, which warns)
globalThis.location ??= { origin: SERVER, href: SERVER + '/', hash: '' }
globalThis.WebSocket ??= { CONNECTING: 0, OPEN: 1, CLOSING: 2, CLOSED: 3 }
const load = async (entry) => {
  const name = path.basename(entry, '.ts') + '.mjs'
  await build({ configFile: false, root: web, logLevel: 'warn', build: { ssr: entry, outDir: out, emptyOutDir: false, minify: false, rollupOptions: { output: { entryFileNames: name } } } })
  return import(pathToFileURL(path.join(out, name)).href)
}
const { Replica } = await load('src/replica.ts')
const { Keys, b64d } = await load('src/crypto.ts')
for (const k of ['log', 'info', 'warn', 'debug']) console[k] = () => {} // the cases are full of things a replica warns about
const say = (s) => process.stdout.write(s + '\n')

/** The browser's storage for one replica (the few calls replica.ts makes), in memory. */
function fakeDb() {
  const stores = new Map()
  const store = (s) => { if (!stores.has(s)) stores.set(s, new Map()); return stores.get(s) }
  const request = (fn) => {
    const req = { result: undefined, error: null, onsuccess: null, onerror: null }
    queueMicrotask(() => { try { req.result = fn(); req.onsuccess?.() } catch (e) { req.error = e; req.onerror?.() } })
    return req
  }
  return {
    transaction: (s) => ({ objectStore: () => ({
      get: (k) => request(() => store(s).get(k)),
      put: (v, k) => request(() => { store(s).set(k, v); return k }),
      delete: (k) => request(() => { store(s).delete(k) }),
      getAllKeys: () => request(() => [...store(s).keys()]),
    }) }),
    close() {},
  }
}

function fakeWs() {
  return { readyState: 1, sent: [], closed: false, send(s) { this.sent.push(JSON.parse(s)) }, close() { this.closed = true } }
}

/** The server's uploads, as the case says it answers for each. */
const fakeFetch = (uploads) => async (url) => {
  const u = uploads[String(url).split('/').pop()]
  const status = u ? u.status : 404
  const body = new Uint8Array(Buffer.from(u?.body ?? '', 'hex'))
  return { status, ok: status >= 200 && status < 300, arrayBuffer: async () => body.buffer.slice(body.byteOffset, body.byteOffset + body.byteLength) }
}

async function sentTags(r, ws) {
  const tags = []
  for (const m of ws.sent) {
    if (m.t !== 'p2p') { tags.push(String(m.t)); continue }
    try { tags.push('p2p:' + JSON.parse(new TextDecoder().decode(await r.keys.decrypt(b64d(m.d)))).k) } catch { tags.push('p2p:?') }
  }
  return tags.sort()
}

async function state(r, ws) {
  return {
    files: Object.fromEntries([...r.files.entries()].map(([k, v]) => [k, v.toString()])),
    since: r.since,
    revoked: [...r.keys.revoked].sort(),
    deleted: r.deletedAt !== null,
    denied: r.denied,
    min: r.minVersion,
    serverMode: r.serverMode,
    storeProblem: r.storeProblem,
    closed: ws.closed,
    sent: await sentTags(r, ws),
  }
}

const settle = async () => { for (let i = 0; i < 5; i++) await new Promise((res) => setTimeout(res, 10)) }

const same = (a, b) => JSON.stringify(a) === JSON.stringify(b)
function differences(want, got) {
  const out = []
  for (const [k, v] of Object.entries(want)) {
    const w = k === 'sent' ? [...v].sort() : v
    if (!same(got[k], w) && !(k === 'files' && same(sortKeys(got[k]), sortKeys(w)))) out.push(`${k}: got ${JSON.stringify(got[k])}, want ${JSON.stringify(w)}`)
  }
  return out
}
const sortKeys = (o) => Object.fromEntries(Object.entries(o ?? {}).sort(([a], [b]) => a.localeCompare(b)))

async function runCase(c, links) {
  const keys = Keys.fromFragment('#' + links[c.as])
  const r = new Replica(keys, SERVER)
  const ws = fakeWs()
  Object.assign(r, { db: fakeDb(), persistence: {}, ws, since: c.since })
  globalThis.fetch = fakeFetch(c.uploads)
  const bad = []
  for (const [i, step] of c.steps.entries()) {
    try { await r.handle(structuredClone(step.msg)) } catch (e) { bad.push(`step ${i + 1} raised ${e?.name}: ${e?.message}`) }
    await settle()
    if (step.expect) bad.push(...differences(step.expect, await state(r, ws)).map((d) => `after step ${i + 1}: ${d}`))
  }
  bad.push(...differences(c.expect, await state(r, ws)))
  return bad
}

const v = JSON.parse(fs.readFileSync(path.join(web, '../scripts/replica-vectors.json'), 'utf8'))
const words = process.argv.slice(2).map((w) => w.toLowerCase())
const cases = v.cases.filter((c) => words.every((w) => c.what.toLowerCase().includes(w)))
let failed = 0
for (const c of cases) {
  const bad = await runCase(c, v.links)
  if (bad.length) { failed++; say(`FAIL ${c.what}\n     ${bad.join('\n     ')}`) }
}
say(`replica (web): ${cases.length - failed} of ${cases.length} cases agree${failed ? `, ${failed} don't` : ''}`)
process.exit(failed ? 1 : 0)
