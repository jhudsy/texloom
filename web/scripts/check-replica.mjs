// Runs web/src/replica.ts through scripts/replica-vectors.json, the cases scripts/check_replica.py runs
// texloom/replica.py through: what a replica does with the server's messages (catching up,
// signatures, uploads, revocations, refusals, peers) must be the same in both apps.
//
// Each case gets a fresh replica, with a stand-in connection that records what it sends, a
// stand-in server for files and uploads and an in-memory stand-in for the browser's storage.
// Nothing touches the network. Besides the server's messages, a case's steps can edit a file
// here, drop and reopen the connection, acknowledge or refuse the last change sent, add a file,
// and fetch one.
//
//     npm run check-replica [words in a case's name]
import fs from 'node:fs'
import path from 'node:path'
import * as Y from 'yjs'
import { browser, load, web } from './lib.mjs'

const SERVER = 'http://test'
browser({ location: { origin: SERVER, href: SERVER + '/', hash: '' }, WebSocket: { CONNECTING: 0, OPEN: 1, CLOSING: 2, CLOSED: 3 } })
const [{ Replica }, { Keys, b64d }, { unpack }] = await load('src/replica.ts', 'src/crypto.ts', 'src/packing.ts')
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

/** The server's files: what it answers for each (the case's `uploads`), and what it's sent
 *  (answered as the case's `put` says), checked as the server would: signed by a writer, and
 *  the content the id names. */
function fakeServer(c, keys) {
  const received = []
  const answer = c.put ?? { status: 200 }
  const respond = (status, body) => ({ status, ok: status >= 200 && status < 300, json: async () => JSON.parse(new TextDecoder().decode(body) || 'null'),
    arrayBuffer: async () => body.buffer.slice(body.byteOffset, body.byteOffset + body.byteLength) })
  const fetch = async (url, init = {}) => {
    const id = String(url).split('/').pop()
    const method = init.method ?? 'GET'
    if (method === 'PUT') {
      const data = new Uint8Array(init.body), h = init.headers ?? {}
      let ok = false
      try { ok = keys.verify(`blob:${id}:`, data, b64d(h['X-Sig'] ?? '')) && (await keys.blobId(await keys.decrypt(data))) === id } catch { ok = false }
      received.push({ id, output: h['X-Kind'] === 'output', ok })
      return respond(answer.status, new TextEncoder().encode(answer.code ? JSON.stringify({ code: answer.code }) : ''))
    }
    if (method !== 'GET') return respond(204, new Uint8Array())
    const u = c.uploads[id]
    return respond(u ? u.status : 404, new Uint8Array(Buffer.from(u?.body ?? '', 'hex')))
  }
  return { fetch, received }
}

async function sentTags(r, ws) {
  const tags = []
  for (const m of ws.sent) {
    if (m.t !== 'p2p') { tags.push(String(m.t)); continue }
    try { tags.push('p2p:' + JSON.parse(new TextDecoder().decode(await r.keys.decrypt(b64d(m.d)))).k) } catch { tags.push('p2p:?') }
  }
  return tags.sort()
}

/** The files the changes sent to the server make, applied to an empty document. */
async function sentFiles(r, socks) {
  const d = new Y.Doc()
  for (const ws of socks) for (const m of ws.sent) if (m.t === 'up') Y.applyUpdate(d, await unpack(await r.keys.decrypt(b64d(m.d))))
  return Object.fromEntries([...d.getMap('files').entries()].map(([k, v]) => [k, v.toString()]))
}
const count = async (r, store) => new Promise((res) => { const q = r.db.transaction(store).objectStore(store).getAllKeys(); q.onsuccess = () => res(q.result.length) })

async function state(r, socks, server, got) {
  const ws = socks[socks.length - 1]
  const sent = []
  for (const w of socks) sent.push(...await sentTags(r, w))
  return {
    files: Object.fromEntries([...r.files.entries()].map(([k, v]) => [k, v.toString()])),
    since: r.since,
    revoked: [...r.keys.revoked].sort(),
    deleted: r.deletedAt !== null,
    denied: r.denied,
    min: r.minVersion,
    serverMode: r.serverMode,
    storeProblem: r.storeProblem,
    closed: socks.some((w) => w.closed),
    sent: sent.sort(),
    pending: await count(r, 'outbox'),
    sentFiles: await sentFiles(r, socks),
    uploaded: [...server.received].sort((a, b) => a.id.localeCompare(b.id) || Number(a.output) - Number(b.output)),
    pendingFiles: await count(r, 'pending'),
    got: got.v ?? null,
    last: r.last,
    lastSeq: r.last ? r.last[0] : null,
    helloLast: [...socks].reverse().flatMap((w) => [...w.sent].reverse()).find((m) => m.t === 'hello')?.last ?? null,
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

const lastUp = (socks) => socks.flatMap((w) => w.sent).filter((m) => m.t === 'up').pop()?.id ?? 'none'

async function runCase(c, links) {
  const keys = Keys.fromFragment('#' + links[c.as])
  const r = new Replica(keys, SERVER)
  const socks = [fakeWs()]
  // connected, as if hello had gone (the cases start from there); edits made here go out as in open()
  Object.assign(r, { db: fakeDb(), persistence: {}, ws: socks[0], since: c.since, status: 'connecting' })
  r.watchLocal()
  const server = fakeServer(c, keys)
  globalThis.fetch = server.fetch
  const got = {}
  let fetching = null
  const look = async () => {
    await settle()
    if (fetching && !fetching.done) got.v = 'waiting'
    return state(r, socks, server, got)
  }
  const bad = []
  for (const [i, step] of c.steps.entries()) {
    try {
      if (step.msg) await r.handle(structuredClone(step.msg))
      else if (step.edit) {
        const { file, append } = step.edit
        r.doc.transact(() => { const t = r.files.get(file); if (t) t.insert(t.length, append); else r.files.set(file, new Y.Text(append)) })
      } else if ('ackLast' in step) await r.handle({ t: 'ack', id: lastUp(socks), q: step.ackLast })
      else if (step.refuseLast) await r.handle({ t: 'err', code: step.refuseLast, id: lastUp(socks), m: 'refused' })
      else if (step.reconnect) { r.dropped(); socks.push(fakeWs()); r.opened(socks[socks.length - 1]) }
      else if (step.putFile) await r.putBlob(new Uint8Array(Buffer.from(step.putFile.hex, 'hex')), step.putFile.kind ?? 'file')
      else if (step.getFile) {
        delete got.v
        const f = { done: false }
        fetching = f
        r.getBlob(step.getFile).then((b) => { got.v = Buffer.from(b).toString('hex') }, () => { got.v = 'unavailable' }).finally(() => { f.done = true })
      }
    } catch (e) { bad.push(`step ${i + 1} raised ${e?.name}: ${e?.message}`) }
    await settle()
    if (step.expect) bad.push(...differences(step.expect, await look()).map((d) => `after step ${i + 1}: ${d}`))
  }
  bad.push(...differences(c.expect, await look()))
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
