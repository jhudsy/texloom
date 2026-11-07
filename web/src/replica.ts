// A full local replica of a project in the browser. Mirrors texloom/replica.py.
//
// Two ways changes travel (both end-to-end encrypted):
//  - via the server's stored log ("server" storage mode): works even if collaborators
//    are never online at the same time;
//  - directly between connected peers, relayed but never stored ("p2p" messages): on
//    connect, peers exchange Yjs state vectors and send each other what's missing.
//    In "relay" storage mode this is the only way, so peers must overlap in time.
import * as Y from 'yjs'
import { IndexeddbPersistence } from 'y-indexeddb'
import * as awarenessProtocol from 'y-protocols/awareness'
import { Keys, b64d, b64e, canonicalPub, revokeMsg, signer } from './crypto'

const REMOTE = Symbol('remote')
const COMPACT_AFTER = 300
// Keep in sync with server/relay.py: frames are at most 16 MiB (base64 adds a third).
const MAX_SNAPSHOT = 12 * 1024 * 1024
const P2P_BLOB = 11 * 1024 * 1024 // files sent peer to peer (relay-only projects)
export const BLOB_ID = /^[0-9a-f]{32}$/
/** Awareness colours and names come from anyone with the link: keep them harmless. */
const COLOR = /^#[0-9a-fA-F]{3,8}$/

// ---- tiny IndexedDB key-value helper (one DB per project) -----------------
type Store = 'kv' | 'outbox' | 'blobs' | 'pending'
function openDb(name: string): Promise<IDBDatabase> {
  return new Promise((res, rej) => {
    const r = indexedDB.open(name, 1)
    r.onupgradeneeded = () => {
      for (const s of ['kv', 'outbox', 'blobs', 'pending']) r.result.createObjectStore(s)
    }
    r.onsuccess = () => res(r.result)
    r.onerror = () => rej(r.error)
  })
}
function tx<T>(db: IDBDatabase, store: Store, mode: IDBTransactionMode, fn: (s: IDBObjectStore) => IDBRequest<T>): Promise<T> {
  return new Promise((res, rej) => {
    const req = fn(db.transaction(store, mode).objectStore(store))
    req.onsuccess = () => res(req.result)
    req.onerror = () => rej(req.error)
  })
}
const idbGet = <T>(db: IDBDatabase, s: Store, k: string) => tx<T>(db, s, 'readonly', (o) => o.get(k))
const idbPut = (db: IDBDatabase, s: Store, k: string, v: unknown) => tx(db, s, 'readwrite', (o) => o.put(v, k))
const idbDel = (db: IDBDatabase, s: Store, k: string) => tx(db, s, 'readwrite', (o) => o.delete(k))
const idbKeys = (db: IDBDatabase, s: Store) => tx<IDBValidKey[]>(db, s, 'readonly', (o) => o.getAllKeys())

const enc = new TextEncoder(), dec = new TextDecoder()
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms))

/** Delete this browser's copy of a project (document, outbox, cached files).
 *  Other people's copies, folder copies and the server's copy are not affected. */
export async function forgetLocalCopy(project: string): Promise<void> {
  const del = (name: string) => new Promise<void>((res, rej) => {
    const r = indexedDB.deleteDatabase(name)
    r.onsuccess = () => res()
    r.onerror = () => rej(r.error)
    r.onblocked = () => res() // another tab still has it open; deleted once that tab closes
  })
  await Promise.all([del(`texloom-${project}`), del(`texloom-doc-${project}`)])
}

/** Creation code for storing new projects on this server (if it requires one). */
export const creationCode = {
  get(): string { try { return localStorage.getItem('texloom.code') || '' } catch { return '' } },
  set(v: string) { try { localStorage.setItem('texloom.code', v) } catch {} },
}

/** Why the server isn't storing this project's changes (they still reach people online). */
export type StoreProblem = 'needs-code' | 'quota-full' | 'project-full' | 'server-full' | null
const STORE_PROBLEMS = new Set(['needs-code', 'quota-full', 'project-full', 'server-full'])
export interface StoreInfo { registered: boolean; creation: 'open' | 'code'; canCreate: boolean; used: number; projectMax: number | null; allowance: number | null }

export type Status = 'offline' | 'connecting' | 'online' | 'paused'
export type SyncMode = 'live' | 'manual'
export type StorageMode = 'server' | 'relay'

export class Replica {
  // gc off: deleted text is kept, so past versions can be reconstructed (History)
  readonly doc = new Y.Doc({ gc: false })
  readonly files = this.doc.getMap<Y.Text>('files')
  readonly bin = this.doc.getMap<{ h: string; size: number }>('bin')
  readonly dirs = this.doc.getMap<boolean>('dirs') // explicit (possibly empty) folders
  readonly versions = this.doc.getArray<any>('versions') // saved versions: {at, by, label, auto, snap}
  readonly members = this.doc.getMap<any>('members') // personal links: public key -> {name, at, by, revoked?}
  /** This personal link was revoked by the owner. */
  withdrawn = false
  readonly clients = this.doc.getMap<string>('clients') // Yjs client id -> display name (for "who wrote this")
  readonly review = this.doc.getMap<Y.Map<any>>('review') // comments and suggested changes (review.ts)
  /** Called after every change made on this device (not ones received from others). */
  readonly localChange: (() => void)[] = []
  readonly meta = this.doc.getMap<any>('meta')
  readonly out = this.doc.getMap<any>('out')
  readonly awareness = new awarenessProtocol.Awareness(this.doc)
  status: Status = 'offline'
  pending = 0 // local updates not yet acknowledged
  peers = 0 // other connections to this project right now
  serverMode: StorageMode = 'server'
  storeProblem: StoreProblem = null
  storeInfo: StoreInfo | null = null
  onStatus: () => void = () => {}
  onCompileAck: (info: { by: string }) => void = () => {}

  private db!: IDBDatabase
  private persistence!: IndexeddbPersistence
  private ws: WebSocket | null = null
  private since = 0
  private seen = new Set<number>()
  /** The server's log (and its list of revocations) has been applied since connecting. */
  private synced = false
  private inflight = new Map<string, string[]>() // sent id -> outbox ids it covers
  private blobWaiters = new Map<string, ((b: Uint8Array) => void)[]>()
  private backoff = 1000
  private closed = false
  private manualDone: (() => void) | null = null

  constructor(readonly keys: Keys, readonly server: string, public syncMode: SyncMode = 'live') {}

  get storage(): StorageMode { return this.meta.get('storage') === 'relay' ? 'relay' : 'server' }

  async open(): Promise<void> {
    this.db = await openDb(`texloom-${this.keys.project}`)
    this.persistence = new IndexeddbPersistence(`texloom-doc-${this.keys.project}`, this.doc)
    await this.persistence.whenSynced
    this.since = (await idbGet<number>(this.db, 'kv', 'since')) ?? 0
    // revocations learned earlier, before anything arrives from peers
    this.learnRevoked((await idbGet<{ p: string; s: string }[]>(this.db, 'kv', 'revoked')) ?? [], false)
    this.pending = (await idbKeys(this.db, 'outbox')).length
    navigator.storage?.persist?.()

    this.doc.on('update', (update: Uint8Array, origin: unknown) => {
      if (origin === REMOTE || origin === this.persistence) return
      if (!this.keys.writable) return
      for (const fn of this.localChange) fn()
      const id = crypto.randomUUID()
      this.pending++
      this.onStatus()
      idbPut(this.db, 'outbox', id, update).then(() => this.send(id, update, [id]))
    })
    // Registered before the editor's own listeners, so they only ever see cleaned states.
    this.awareness.on('change', ({ added, updated }: any, origin: unknown) => {
      if (origin !== REMOTE) return
      for (const id of [...added, ...updated]) cleanPresence(this.awareness.getStates().get(id))
    })
    this.awareness.on('update', ({ added, updated, removed }: any, origin: unknown) => {
      if (origin === REMOTE) return
      const changed = added.concat(updated, removed)
      this.sendAwareness(awarenessProtocol.encodeAwarenessUpdate(this.awareness, changed))
    })
    if (this.syncMode === 'live') this.connect()
    else { this.status = 'paused'; this.onStatus() }
  }

  async close() {
    this.closed = true
    awarenessProtocol.removeAwarenessStates(this.awareness, [this.doc.clientID], 'close')
    this.ws?.close()
    await this.persistence.destroy()
    this.db?.close()
  }

  // ---- live vs manual sync ---------------------------------------------------
  setSyncMode(mode: SyncMode) {
    this.syncMode = mode
    const ws = this.ws as WebSocket | null
    if (mode === 'live' && !ws) this.connect()
    if (mode === 'manual') { (ws as WebSocket | null)?.close(); this.status = 'paused'; this.onStatus() }
  }

  /** Manual mode: connect, exchange everything, then disconnect again. */
  async syncNow(): Promise<void> {
    if (this.ws) return
    const done = new Promise<void>((r) => (this.manualDone = r))
    this.connect()
    await Promise.race([done, sleep(30000)])
    // give peers a moment to answer our state vector, and the server to ack our updates
    for (let i = 0; i < 20 && (this.inflight.size || i < 6); i++) await sleep(250)
    if (this.syncMode === 'manual') (this.ws as WebSocket | null)?.close()
  }

  // ---- connection ----------------------------------------------------------
  private connect() {
    if (this.closed) return
    const url = this.server.replace(/^http/, 'ws') + `/ws/${this.keys.project}`
    this.status = 'connecting'; this.onStatus()
    const ws = new WebSocket(url)
    this.ws = ws
    ws.onopen = () => {
      this.backoff = 1000
      const code = creationCode.get()
      ws.send(JSON.stringify({ t: 'hello', since: this.since, ...(code ? { code } : {}) }))
    }
    ws.onmessage = (ev) => this.handle(JSON.parse(ev.data)).catch((e) => console.error(e))
    ws.onclose = () => {
      this.ws = null
      this.synced = false
      this.inflight.clear()
      this.peers = 0
      this.status = this.syncMode === 'manual' ? 'paused' : 'offline'; this.onStatus()
      if (!this.closed && this.syncMode === 'live') setTimeout(() => this.connect(), (this.backoff = Math.min(this.backoff * 2, 30000)))
    }
  }

  private async handle(m: any) {
    switch (m.t) {
      case 'log':
        this.serverMode = m.mode
        this.peers = m.peers ?? 0
        this.storeInfo = m.store ?? null
        this.learnRevoked(m.rv ?? [])
        if (this.storeInfo?.registered && this.storeProblem === 'needs-code') this.storeProblem = null
        // the server dropped its copy (idle eviction): everything up to `floor` is gone
        const lost = m.mode !== 'relay' && (m.head < this.since || (this.since > 0 && this.since <= (m.floor ?? 0)))
        if (lost) { this.since = m.head; this.seen.clear(); idbPut(this.db, 'kv', 'since', this.since) }
        await this.apply(m.entries)
        this.since = Math.max(this.since, m.head)
        if (lost && this.keys.writable) await this.reseed()
        for (const q of this.seen) if (q <= this.since) this.seen.delete(q)
        this.markSeen(this.since)
        this.synced = true
        this.status = 'online'; this.onStatus()
        const pendingMode = await idbGet<StorageMode>(this.db, 'kv', 'pendingMode')
        if (this.keys.writable && pendingMode) await this.sendMode(pendingMode) // changed while offline
        await this.uploadPending()
        await this.flushOutbox()
        await this.p2p({ k: 'sv1', sv: b64e(Y.encodeStateVector(this.doc)) })
        this.sendAwareness(awarenessProtocol.encodeAwarenessUpdate(this.awareness, [this.doc.clientID]))
        if (m.count >= COMPACT_AFTER) await this.compact()
        this.manualDone?.(); this.manualDone = null
        break
      case 'up':
        await this.apply([m])
        if (m.q) this.markSeen(m.q)
        break
      case 'ack': {
        if (m.q) this.markSeen(m.q)
        if (m.q && this.storeProblem) { this.storeProblem = null; this.onStatus() }
        for (const id of this.inflight.get(m.id) ?? []) await idbDel(this.db, 'outbox', id)
        this.inflight.delete(m.id)
        this.pending = (await idbKeys(this.db, 'outbox')).length
        this.onStatus()
        break
      }
      case 'aw': {
        const upd = await this.keys.decrypt(b64d(m.d)).catch(() => null)
        if (upd) try { awarenessProtocol.applyAwarenessUpdate(this.awareness, upd, REMOTE) } catch { break }
        this.peers = Math.max(this.peers, this.awareness.getStates().size - 1)
        this.onStatus()
        break
      }
      case 'p2p':
        await this.onP2p(m)
        break
      case 'rv':
        this.learnRevoked([m])
        break
      case 'mode':
        this.serverMode = m.m
        if (m.m === 'relay') this.storeProblem = null // not storing is the point
        this.onStatus()
        break
      case 'err':
        if (STORE_PROBLEMS.has(m.code)) {
          // Not stored, but already relayed to anyone online. Keep it in the outbox; retry later.
          if (m.id) this.inflight.delete(m.id)
          this.storeProblem = m.code
          this.scheduleRetry()
          this.onStatus()
        } else console.warn('server:', m.m)
    }
  }

  private retryTimer: ReturnType<typeof setTimeout> | null = null
  private scheduleRetry() {
    if (this.retryTimer || this.storeProblem === 'needs-code') return // a code only helps once entered
    this.retryTimer = setTimeout(() => { this.retryTimer = null; if (this.status === 'online') this.flushOutbox() }, 5 * 60_000)
  }

  /** Reconnect (e.g. after entering a creation code) so the server re-evaluates storing. */
  retryStore() {
    this.storeProblem = null
    this.onStatus()
    if (this.syncMode === 'manual') { this.syncNow(); return }
    ;(this.ws as WebSocket | null)?.close() // onclose reconnects; hello carries the new code
  }

  private markSeen(q: number) {
    if (q > this.since) this.seen.add(q)
    while (this.seen.has(this.since + 1)) this.seen.delete(++this.since)
    idbPut(this.db, 'kv', 'since', this.since)
  }

  private async apply(entries: { q: number; d: string; s: string; snap?: boolean }[]) {
    for (const e of entries) {
      const data = b64d(e.d), sig = b64d(e.s)
      if (!this.keys.verify(e.snap ? `snap:${e.q}:` : 'upd:', data, sig)) {
        console.warn('dropping update with bad signature', e.q); continue
      }
      try {
        Y.applyUpdate(this.doc, await this.keys.decrypt(data), REMOTE)
      } catch (err) {
        console.warn('dropping undecryptable update', e.q, err)
      }
    }
  }

  private async send(id: string, update: Uint8Array, covers: string[]) {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN || this.status !== 'online') return
    const d = await this.keys.encrypt(update)
    this.inflight.set(id, covers)
    this.ws.send(JSON.stringify({ t: 'up', id, d: b64e(d), s: b64e(this.keys.sign('upd:', d)) }))
  }

  private async flushOutbox() {
    if (!this.keys.writable) return
    const ids = (await idbKeys(this.db, 'outbox')) as string[]
    if (!ids.length) return
    const updates = await Promise.all(ids.map((id) => idbGet<Uint8Array>(this.db, 'outbox', id)))
    await this.send(crypto.randomUUID(), Y.mergeUpdates(updates), ids)
  }

  private async compact() {
    if (!this.keys.writable || !this.ws || this.serverMode === 'relay') return
    const d = await this.keys.encrypt(Y.encodeStateAsUpdate(this.doc))
    if (d.length > MAX_SNAPSHOT) return // too big for the server to take: the log stays as it is
    const upto = this.since
    this.ws.send(JSON.stringify({ t: 'snap', upto, d: b64e(d), s: b64e(this.keys.sign(`snap:${upto}:`, d)) }))
  }

  private async sendAwareness(update: Uint8Array) {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return
    this.ws.send(JSON.stringify({ t: 'aw', d: b64e(await this.keys.encrypt(update)) }))
  }

  // ---- peer-to-peer (relayed, never stored) ------------------------------------
  private async p2p(payload: Record<string, unknown>, to?: number) {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return
    const d = await this.keys.encrypt(enc.encode(JSON.stringify(payload)))
    const msg: any = { t: 'p2p', d: b64e(d) }
    if (payload.k === 'upd') msg.s = b64e(this.keys.sign('p2p:', d))
    if (to !== undefined) msg.to = to
    this.ws.send(JSON.stringify(msg))
  }

  private async onP2p(m: { d: string; s?: string; from: number }) {
    const data = b64d(m.d)
    let p: any
    try { p = JSON.parse(dec.decode(await this.keys.decrypt(data))) } catch { return }
    switch (p.k) {
      case 'sv1': // a peer just connected: send what it lacks, and ask for what we lack
      case 'sv2': {
        this.peers = Math.max(this.peers, 1); this.onStatus()
        if (this.keys.writable) {
          const diff = Y.encodeStateAsUpdate(this.doc, b64d(p.sv))
          if (diff.length > 2) await this.p2p({ k: 'upd', u: b64e(diff) }, m.from)
        }
        if (p.k === 'sv1') await this.p2p({ k: 'sv2', sv: b64e(Y.encodeStateVector(this.doc)) }, m.from)
        break
      }
      case 'upd':
        // Not before the server's list of revocations is in: a revoked link could slip one in.
        // (Peers resend after our state vector, which goes out once the log is applied.)
        if (!this.synced) return
        if (!m.s || !this.keys.verify('p2p:', data, b64d(m.s), true)) { console.warn('unsigned p2p update ignored'); return }
        Y.applyUpdate(this.doc, b64d(p.u), REMOTE)
        break
      case 'breq': {
        if (typeof p.id !== 'string' || !BLOB_ID.test(p.id)) return
        const blob = await idbGet<Uint8Array>(this.db, 'blobs', p.id)
        if (blob && blob.length <= P2P_BLOB) await this.p2p({ k: 'blob', id: p.id, b: b64e(blob) }, m.from)
        break
      }
      case 'cack': // a texloom helper accepted our compile request
        this.onCompileAck(p)
        break
      case 'blob': {
        // only files we asked for (anyone with the link can send these)
        if (typeof p.id !== 'string' || !this.blobWaiters.has(p.id) || typeof p.b !== 'string') return
        const blob = b64d(p.b)
        if ((await this.keys.blobId(blob)) !== p.id) return // integrity check
        await idbPut(this.db, 'blobs', p.id, blob)
        for (const w of this.blobWaiters.get(p.id) ?? []) w(blob)
        this.blobWaiters.delete(p.id)
      }
    }
  }

  /** Ask connected texloom helpers (running with --compile) to compile now. */
  async requestCompile(): Promise<boolean> {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return false
    await this.p2p({ k: 'creq' })
    return true
  }

  // ---- storage mode -----------------------------------------------------------
  /** Personal links the owner revoked (each signed by the owner); kept on this device too. */
  private learnRevoked(items: { p: string; s: string }[], save = true) {
    let added = false
    for (const it of items) {
      try {
        const pub = canonicalPub(it?.p)
        if (!pub || this.keys.revoked.has(pub) || typeof it.s !== 'string') continue
        const who = signer(this.keys.project, revokeMsg(pub), b64d(it.s))
        if (!who || !('owner' in who)) continue
        this.keys.revoked.add(pub)
        this.revocations.push({ p: pub, s: it.s })
        added = true
        if (pub === this.keys.memberPub && !this.withdrawn) {
          this.withdrawn = true
          try { localStorage.setItem(`texloom.withdrawn.${this.keys.project}`, '1') } catch {}
          this.onStatus()
        }
      } catch {}
    }
    if (added && save) idbPut(this.db, 'kv', 'revoked', this.revocations).catch(() => {})
  }
  private revocations: { p: string; s: string }[] = []

  /** Owner only: withdraw a personal edit link. */
  revoke(pub: string): boolean {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return false
    this.ws.send(JSON.stringify({ t: 'revoke', ...this.keys.revocation(pub) }))
    return true
  }

  private async sendMode(mode: StorageMode) {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return
    const ts = Math.floor(Date.now() / 1000)
    this.ws.send(JSON.stringify({ t: 'mode', m: mode, ts, s: b64e(this.keys.sign(`mode:${mode}:${ts}`, new Uint8Array())) }))
    this.serverMode = mode
    if (mode === 'relay') this.storeProblem = null
    await idbDel(this.db, 'kv', 'pendingMode')
    if (mode === 'server') {
      // Give the server a complete copy again: full state plus every blob we hold.
      await this.send(crypto.randomUUID(), Y.encodeStateAsUpdate(this.doc), [])
      for (const id of (await idbKeys(this.db, 'blobs')) as string[]) await idbPut(this.db, 'pending', id, 1)
      await this.uploadPending()
    }
    this.onStatus()
  }

  /** Change where the project lives. 'relay' deletes the server's copy. */
  async setStorage(mode: StorageMode) {
    if (!this.keys.writable) throw new Error('read-only')
    if (mode === 'relay' && !this.keys.owner) throw new Error('only the owner can remove the server’s copy')
    this.meta.set('storage', mode)
    await idbPut(this.db, 'kv', 'pendingMode', mode)
    await this.sendMode(mode)
  }

  // ---- blobs ---------------------------------------------------------------
  async putBlob(plain: Uint8Array): Promise<string> {
    const id = await this.keys.blobId(plain)
    if (!(await idbGet(this.db, 'blobs', id))) {
      await idbPut(this.db, 'blobs', id, plain)
      await idbPut(this.db, 'pending', id, 1)
    }
    if (this.status === 'online') await this.uploadPending()
    return id
  }

  /** Give the server back its copy: the whole document, and every file we have. */
  private async reseed() {
    console.info('the server has no copy of this project any more: re-uploading it')
    for (const [, b] of this.bin) if (b?.h && (await idbGet(this.db, 'blobs', b.h))) await idbPut(this.db, 'pending', b.h, 1)
    await this.uploadPending()
    await this.send(crypto.randomUUID(), Y.encodeStateAsUpdate(this.doc), [])
  }

  private async uploadPending() {
    if (!this.keys.writable || this.serverMode === 'relay' || this.storage === 'relay') return
    for (const id of (await idbKeys(this.db, 'pending')) as string[]) {
      const plain = await idbGet<Uint8Array>(this.db, 'blobs', id)
      const d = await this.keys.encrypt(plain)
      const code = creationCode.get()
      const r = await fetch(`${this.server}/blob/${this.keys.project}/${id}`, {
        method: 'PUT', body: d as BodyInit,
        headers: { 'X-Sig': b64e(this.keys.sign(`blob:${id}:`, d)), ...(code ? { 'X-Create-Code': code } : {}) },
      }).catch(() => null)
      if (r && [403, 413, 507].includes(r.status)) {
        const why = (await r.json().catch(() => ({}))).code
        if (STORE_PROBLEMS.has(why)) { this.storeProblem = why; this.scheduleRetry(); this.onStatus() }
      }
      if (!r?.ok) return
      await idbDel(this.db, 'pending', id)
    }
  }

  async getBlob(id: string): Promise<Uint8Array> {
    if (!BLOB_ID.test(id)) throw new Error('not a file id') // ids come from the shared document
    const cached = await idbGet<Uint8Array>(this.db, 'blobs', id)
    if (cached) return cached
    if (this.serverMode === 'server') {
      const r = await fetch(`${this.server}/blob/${this.keys.project}/${id}`).catch(() => null)
      if (r?.ok) {
        const plain = await this.keys.decrypt(new Uint8Array(await r.arrayBuffer()))
        // The id is an HMAC of the content: the server can't substitute another file.
        if ((await this.keys.blobId(plain)) === id) {
          await idbPut(this.db, 'blobs', id, plain)
          return plain
        }
        console.warn('server returned the wrong content for', id)
      }
    }
    // Ask connected peers for it.
    const got = new Promise<Uint8Array>((res) => this.blobWaiters.set(id, [...(this.blobWaiters.get(id) ?? []), res]))
    await this.p2p({ k: 'breq', id })
    const r = await Promise.race([got, sleep(20000).then(() => null)])
    if (!r) throw new Error(`file content not available yet (${id.slice(0, 8)}): no online peer has it`)
    return r
  }

  /** All project files as {path: content}, for compiling or exporting. */
  async snapshotFiles(): Promise<Map<string, string | Uint8Array>> {
    const out = new Map<string, string | Uint8Array>()
    this.files.forEach((t, p) => out.set(p, t.toString()))
    for (const [p, b] of this.bin.entries()) out.set(p, await this.getBlob(b.h))
    return out
  }
}

/** A peer's presence state, made harmless: names are text, colours must be colours. */
function cleanPresence(st: any) {
  const u = st?.user
  if (!u || typeof u !== 'object') return
  if (typeof u.name !== 'string') u.name = 'Someone'
  u.name = u.name.slice(0, 60)
  for (const k of ['color', 'colorLight']) if (k in u && (typeof u[k] !== 'string' || !COLOR.test(u[k]))) u[k] = '#888888'
}
