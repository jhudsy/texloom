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
import { Keys, b64d, b64e, canonicalPub, revokeMsg } from './crypto'
import { packed, unpack } from './packing'
import { t } from './i18n'
import { APP_VERSION } from './version'
import type { Link } from './versions'
import { quiet } from './quiet'
import { randomHex } from './bytes'
import { local } from './store'
import { textStore } from './accounts'
import { connect, req } from './idb'
import { debounce, sleep } from './dom'

const REMOTE = Symbol('remote')
const COMPACT_AFTER = 300
// Keep in sync with server/relay.py: frames are at most 16 MiB (base64 adds a third).
const MAX_SNAPSHOT = 12 * 1024 * 1024
const UPDATE_BATCH = 6 * 1024 * 1024 // pending changes are merged up to this much per message
// A bigger change (a whole project, say) or snapshot goes to the server as an encrypted upload,
// like a file, and what's sent is a small reference to it: REF + {"b": upload id}. Uploads go as
// "output", so the writer whose snapshot replaces them may delete them. As in texloom/replica.py.
const enc = new TextEncoder(), dec = new TextDecoder()
const REF = enc.encode('\x00texloom-ref\x00')
const isRef = (u: Uint8Array) => u.length > REF.length && REF.every((b, i) => u[i] === b)
const refTo = (id: string) => { const j = enc.encode(JSON.stringify({ b: id })), u = new Uint8Array(REF.length + j.length); u.set(REF); u.set(j, REF.length); return u }
const SIGNED_P2P = new Set(['upd', 'creq']) // peer-to-peer messages that carry the sender's signature (the desktop signs cack, cerr)
const ANSWER_EVERY = 30_000 // ms: how often one connection's request for what it lacks is answered (onP2p)
const P2P_BLOB = 50 * 1024 * 1024 // files sent peer to peer (relay-only projects): as big as any synced file
// A peer-to-peer message bigger than this (a whole project for a peer catching up, a file) goes
// in pieces: the server takes frames of at most 16 MiB. As in texloom/replica.py.
const P2P_PIECE = 4 * 1024 * 1024
const P2P_PARTS = 32, P2P_ASSEMBLING = 4
// Unfinished messages are held in memory, so anyone with a link could send pieces that never
// end: at most this much in all (oldest dropped first), two at a time from one sender, and none
// that has gone quiet for P2P_STALE ms.
const P2P_HELD = 128 * 1024 * 1024, P2P_PER_SENDER = 2, P2P_STALE = 60_000
// The server closes a connection sending more than 160 MiB in 10 s: pieces are paced below that.
const P2P_WINDOW = 10_000, P2P_RATE = 96 * 1024 * 1024
export const BLOB_ID = /^[0-9a-f]{32}$/
/** Awareness colours and names come from anyone with the link: keep them harmless. */
const COLOR = /^#[0-9a-fA-F]{3,8}$/

// ---- tiny IndexedDB key-value helper (one DB per project) -----------------
type Store = 'kv' | 'outbox' | 'blobs' | 'pending'
// (a connection of its own, closed with the replica: another copy of the project may be open too)
const openDb = (name: string) => connect(name, 1, (db) => { for (const s of ['kv', 'outbox', 'blobs', 'pending']) db.createObjectStore(s) })
const tx = <T>(db: IDBDatabase, store: Store, mode: IDBTransactionMode, fn: (s: IDBObjectStore) => IDBRequest<T>): Promise<T> =>
  req(fn(db.transaction(store, mode).objectStore(store)))
const idbGet = <T>(db: IDBDatabase, s: Store, k: string) => tx<T>(db, s, 'readonly', (o) => o.get(k))
const idbPut = (db: IDBDatabase, s: Store, k: string, v: unknown) => tx(db, s, 'readwrite', (o) => o.put(v, k))
const idbDel = (db: IDBDatabase, s: Store, k: string) => tx(db, s, 'readwrite', (o) => o.delete(k))
const idbKeys = (db: IDBDatabase, s: Store) => tx<IDBValidKey[]>(db, s, 'readonly', (o) => o.getAllKeys())


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

/** Creation codes for storing new projects on a server (if it requires one): one per server, this
 *  app's own by default. Each follows the project list (listsync.ts), with when it was entered. */
const codeKey = (server: string) => (server === location.origin ? 'texloom.code' : `texloom.code@${server}`)
export const creationCode = {
  get: (server = location.origin): string => textStore(codeKey(server)).get(),
  /** When it was entered here (0: before this was recorded), so the latest one follows the project list. */
  at: (server = location.origin): number => textStore(codeKey(server)).at(),
  set: (v: string, at = Date.now(), server = location.origin) => textStore(codeKey(server)).set(v, at),
  /** The other servers this browser has a code for. */
  servers: (): string[] => local.keys().filter((k) => k.startsWith('texloom.code@') && !k.endsWith('At') && local.get(k)).map((k) => k.slice('texloom.code@'.length)).sort(),
}

/** Why the server isn't storing this project's changes (they still reach people online). */
export type StoreProblem = 'needs-code' | 'quota-full' | 'code-expired' | 'project-full' | 'server-full' | null
const STORE_PROBLEMS = new Set(['needs-code', 'quota-full', 'code-expired', 'project-full', 'server-full'])
/** What the server keeps of this project, and its limits. `ownerUsed`: all that the project's creation code
 *  stores (its other projects and project lists too), only for a browser holding that code. */
export interface StoreInfo { registered: boolean; creation: 'open' | 'code'; canCreate: boolean; used: number; projectMax: number | null; allowance: number | null; ownerUsed: number | null }

export type Status = 'offline' | 'connecting' | 'online' | 'paused'
export type SyncMode = 'live' | 'manual'
export type StorageMode = 'server' | 'relay'
/** Why the server didn't move a project to another code (moveToCode). */
export interface MoveRefusal { code: 'move-bad-code' | 'move-no-room' | 'move-too-big' | 'move-not-stored' | 'move-rejected' | 'move-busy'; need?: number; free?: number; max?: number }

/** A change's content hash, as the server records it (to recognise replays, and a restored log). */
async function sha(data: Uint8Array): Promise<string> {
  return b64e(new Uint8Array(await crypto.subtle.digest('SHA-256', data as BufferSource)))
}

export class Replica {
  // gc off: deleted text is kept, so past versions can be reconstructed (History)
  readonly doc = new Y.Doc({ gc: false })
  readonly files = this.doc.getMap<Y.Text>('files')
  readonly bin = this.doc.getMap<{ h: string; size: number }>('bin')
  readonly dirs = this.doc.getMap<boolean>('dirs') // explicit (possibly empty) folders
  readonly versions = this.doc.getArray<any>('versions') // saved versions: {at, by, label, auto, link}
  readonly versionChain = this.doc.getArray<Link>('versionChain') // what those versions are built from (versions.ts)
  // The owner's revocations, in the document too (pub -> signature): a server that holds them back
  // can't keep them from anyone who syncs the document, peer to peer included. As in replica.py.
  readonly revokedDoc = this.doc.getMap<string>('revoked')
  readonly members = this.doc.getMap<any>('members') // personal links: public key -> {name, at, by, g}
  /** This personal link was revoked by the owner. */
  withdrawn = false
  readonly clients = this.doc.getMap<string>('clients') // Yjs client id -> display name (for "who wrote this")
  readonly review = this.doc.getMap<Y.Map<any>>('review') // comments and suggested changes (review.ts)
  /** Called after every change made on this device (not ones received from others). */
  readonly meta = this.doc.getMap<any>('meta')
  readonly out = this.doc.getMap<any>('out')
  readonly awareness = new awarenessProtocol.Awareness(this.doc)
  private presenceConn = new Map<number, number | undefined>() // which server connection each person's presence came through
  private presenceSender: number | undefined
  private presenceQueue: Promise<void> = Promise.resolve()
  status: Status = 'offline'
  pending = 0 // local updates not yet acknowledged
  peers = 0 // other connections to this project right now
  serverMode: StorageMode = 'server'
  private refusal: StoreProblem = null // what the server last said
  storeInfo: StoreInfo | null = null
  /** A private server turned this project away: why. Everything here keeps working on this device. */
  denied: 'needs-code' | 'code-expired' | 'update-needed' | null = null
  /** With update-needed: the oldest version the server accepts. */
  minVersion = ''
  /** When the owner deleted the project for everyone (ms), or null. This copy then stays as it
   *  is, read-only: nothing connects, and nothing is sent. */
  deletedAt: number | null = null
  onDeleted: () => void = () => {}
  /** Why the server isn't storing changes. */
  get storeProblem(): StoreProblem { return this.refusal }
  onStatus: () => void = () => {}
  /** Fresh figures arrived for storeInfo (askUsage). */
  onUsage: () => void = () => {}

  /** Ask the server how much it keeps now: the figures in storeInfo are from when this browser
   *  connected. */
  askUsage(): boolean {
    const ws = this.ws
    if (!ws || ws.readyState !== WebSocket.OPEN || !this.storeInfo) return false
    ws.send(JSON.stringify({ t: 'usage' }))
    return true
  }
  onCompileAck: (info: { by: string }) => void = () => {}
  /** Folders the TeXLoom app syncs on other computers, by folder id: when last heard, whether that
   *  computer keeps the folder in step, its name (texloom/sharedfolder.py; the Dropbox link stands
   *  by while an app keeps the same folder, dropbox.ts). */
  readonly folders = new Map<string, { at: number; holding: boolean; by: string }>()
  private compileNonces: string[] = [] // our last compile requests', which answers must carry
  onCompileError: (info: { by: string; why: string }) => void = () => {}

  private db!: IDBDatabase
  private persistence!: IndexeddbPersistence
  private ws: WebSocket | null = null
  private since = 0
  private seen = new Set<number>()
  // The newest change this copy has from the server: [its number, its SHA-256], sent with each
  // hello so that a server restored from a backup, which numbers changes again, is noticed
  private last: [number, string] | null = null
  private sentHash = new Map<string, string>() // sent id -> its SHA-256, until acknowledged
  /** The server's log (and its list of revocations) has been applied since connecting. */
  private synced = false
  private inflight = new Map<string, string[]>() // sent id -> outbox ids it covers
  private refs: Record<number, string> = {} // stored changes and snapshots that are uploads: seq -> upload id
  private refSent = new Map<string, string>() // sent id -> upload id, until the server stores it
  private snapRef: { upto: number; ref: string | null } | null = null // the snapshot sent, until the server takes it
  private resync = 0 // 1: an upload a change refers to is gone, so catch up from the start (once: then it's skipped)
  // Changes whose upload couldn't be fetched just now (offline, a server error): `since` stays
  // below the first, so it's fetched again on reconnecting, and no snapshot is made meanwhile.
  private missing = new Set<number>()
  private missingTries = 0
  private blobWaiters = new Map<string, ((b: Uint8Array) => void)[]>()
  private backoff = 1000
  private closed = false
  private manualDone: (() => void) | null = null
  /** A catch-up whose later pages are still coming. */
  private logPending: { head: number; count: number; lost: boolean } | null = null
  private pages: Promise<void> = Promise.resolve()

  constructor(readonly keys: Keys, readonly server: string, public syncMode: SyncMode = 'live') {}

  /** A transaction applied changes received from others. */
  isRemote(origin: unknown): boolean { return origin === REMOTE }

  /** A transaction made changes here (not received, and not this browser's copy loading). */
  isLocal(origin: unknown): boolean { return origin !== REMOTE && origin !== this.persistence }

  /** The creation code to show this project's server: the one for that server (servers.ts). */
  private creationCode(): string { return creationCode.get(this.server) }

  /** Changes made now don't reach anyone yet: offline, still catching up, or sync-only with nobody else online. */
  get apart(): boolean { return !this.synced || (this.storage === 'relay' && this.peers === 0) }

  /** Everything made here has reached the server (or, sync-only, the others online), and theirs is in. */
  get exchanged(): boolean { return !this.apart && this.pending === 0 }

  /** Where the project lives, as its owner set it: `meta.storage` is {m, at, s}, signed by the
   *  owner, so an editor can't change it for everyone. Without a valid one (a project from
   *  before 0.4.35, or an editor overwrote it), what the server last said it does decides, as
   *  the server is the one that knows whether it keeps a copy; failing that, the plain value. */
  get storage(): StorageMode {
    const v = this.meta.get('storage')
    if (v && typeof v === 'object' && (v.m === 'relay' || v.m === 'server') && this.keys.ownerSigned(`storage:${v.m}:${v.at}`, v.s)) return v.m
    // compat(data 0.4.35): the plain, unsigned storage value (v == 'relay'); converted on read: its owner signs it
    return this.heardMode ?? (v === 'relay' ? 'relay' : 'server')
  }
  private heardMode: StorageMode | null = null // what the server last said it does with this project (kept)
  private heard(mode: unknown) {
    if (mode !== 'relay' && mode !== 'server') return
    this.serverMode = mode
    if (mode !== this.heardMode) { this.heardMode = mode; idbPut(this.db, 'kv', 'heardMode', mode) }
  }

  async open(): Promise<void> {
    this.db = await openDb(`texloom-${this.keys.project}`)
    this.persistence = new IndexeddbPersistence(`texloom-doc-${this.keys.project}`, this.doc)
    await this.persistence.whenSynced
    this.since = (await idbGet<number>(this.db, 'kv', 'since')) ?? 0
    this.last = (await idbGet<[number, string]>(this.db, 'kv', 'last')) ?? null
    this.refs = (await idbGet<Record<number, string>>(this.db, 'kv', 'refs')) ?? {}
    // revocations learned earlier, before anything arrives from peers
    this.learnRevoked((await idbGet<{ p: string; s: string }[]>(this.db, 'kv', 'revoked')) ?? [], false)
    const fromDoc = () => this.learnRevoked([...this.revokedDoc.entries()].map(([p, s]) => ({ p, s })))
    fromDoc()
    this.revokedDoc.observe(fromDoc)
    this.pending = (await idbKeys(this.db, 'outbox')).length
    this.deletedAt = (await idbGet<number>(this.db, 'kv', 'deleted')) ?? null
    this.heardMode = (await idbGet<StorageMode>(this.db, 'kv', 'heardMode')) ?? null
    navigator.storage?.persist?.()

    this.watchLocal()
    // Registered before the editor's own listeners, so they only ever see cleaned states.
    this.awareness.on('change', ({ added, updated }: any, origin: unknown) => {
      if (origin !== REMOTE) return
      for (const id of [...added, ...updated]) cleanPresence(this.awareness.getStates().get(id))
    })
    this.awareness.on('update', ({ added, updated, removed }: any, origin: unknown) => {
      for (const id of removed) this.presenceConn.delete(id)
      if (origin === REMOTE) {
        for (const id of [...added, ...updated]) this.presenceConn.set(id, this.presenceSender)
        return
      }
      const changed = added.concat(updated, removed)
      this.sendAwareness(awarenessProtocol.encodeAwarenessUpdate(this.awareness, changed))
    })
    this.out.observe(debounce(() => { this.pruneOutputs().catch((e) => quiet('old outputs are pruned at the next change', e)) }, 3000))
    if (this.syncMode === 'live') this.connect()
    else { this.status = 'paused'; this.onStatus() }
  }

  /** Changes made here go into the outbox, and out to the server (scripts: check-replica.mjs calls this too). */
  private watchLocal() {
    this.doc.on('update', (update: Uint8Array, origin: unknown) => {
      if (origin === REMOTE || origin === this.persistence) return
      if (!this.keys.writable || this.deletedAt) return
      const id = crypto.randomUUID()
      this.pending++
      this.onStatus()
      idbPut(this.db, 'outbox', id, update).then(() => this.send(id, update, [id]))
    })
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
  private retry: ReturnType<typeof setTimeout> | undefined
  /** Try again now, after a private server refused and a code has since been entered. */
  reconnect() {
    clearTimeout(this.retry)
    if (!this.ws) this.connect()
  }

  private connect() {
    if (this.closed || this.deletedAt) return
    const url = this.server.replace(/^http/, 'ws') + `/ws/${this.keys.project}`
    this.status = 'connecting'; this.onStatus()
    const ws = new WebSocket(url)
    this.ws = ws
    ws.onopen = () => this.opened(ws)
    ws.onmessage = (ev) => {
      const m = JSON.parse(ev.data), run = () => this.handle(m)
      // a catch-up's pages are applied one after another, in order (other messages don't wait)
      const done = m.t === 'log' || m.t === 'logmore' ? (this.pages = this.pages.then(run, run)) : run()
      done.catch((e) => console.error(e))
    }
    ws.onclose = () => {
      this.dropped()
      if (this.closed || this.deletedAt || this.syncMode !== 'live') return
      // A refusal won't change in seconds; renewing a code or entering one calls reconnect().
      this.retry = setTimeout(() => this.connect(), this.denied ? 5 * 60_000 : (this.backoff = Math.min(this.backoff * 2, 30000)))
    }
  }

  /** A connection to the server is open: start catching up (check-replica.mjs calls this too). */
  private opened(ws: WebSocket) {
    this.ws = ws
    this.backoff = 1000
    const code = this.creationCode()
    this.logPending = null
    this.missing.clear() // (this catch-up finds them again if they're still missing)
    ws.send(JSON.stringify({ t: 'hello', since: this.since, app: 'web', v: APP_VERSION, pages: 1, ...(this.last ? { last: this.last } : {}),
      ...(code ? { code } : {}) }))
  }

  /** The connection closed (check-replica.mjs calls this too). */
  private dropped() {
    this.ws = null
    this.parts.clear()
    this.synced = false
    this.inflight.clear()
    this.peers = 0
    this.status = this.syncMode === 'manual' ? 'paused' : 'offline'; this.onStatus()
  }

  /** The last page of a catch-up is in: what follows from being up to date. */
  private async onCaughtUp(head: number, count: number, lost: boolean) {
    this.since = lost ? head : Math.max(this.since, head)
    this.holdForMissing()
    if (lost && this.keys.writable) await this.reseed()
    for (const q of this.seen) if (q <= this.since) this.seen.delete(q)
    this.markSeen(this.since)
    this.synced = true
    this.status = 'online'; this.onStatus()
    const pendingMode = await idbGet<StorageMode>(this.db, 'kv', 'pendingMode')
    if (this.keys.owner && pendingMode) await this.sendMode(pendingMode) // changed while offline
    // A project from before 0.4.35 has a plain, unsigned value: its owner signs what the server does.
    const v = this.meta.get('storage')
    // compat(data 0.4.35): converted on read: the owner signs an unsigned storage value
    if (this.keys.owner && !pendingMode && v !== undefined && typeof v !== 'object') this.signStorage(this.serverMode)
    await this.uploadPending()
    await this.flushOutbox()
    await this.p2p({ k: 'sv1', sv: b64e(Y.encodeStateVector(this.doc)) })
    this.awareness.setLocalState(this.awareness.getLocalState()) // a newer clock, so it counts as news to everyone; sent by the listener above
    if (count >= COMPACT_AFTER) await this.compact()
    setTimeout(() => this.pruneOutputs().catch((e) => quiet('old outputs are pruned at the next change', e)), 5000)
    this.manualDone?.(); this.manualDone = null
  }

  private async handle(m: any) {
    switch (m.t) {
      case 'log':
        this.denied = null
        this.heard(m.mode)
        this.peers = m.peers ?? 0
        this.storeInfo = m.store ?? null
        this.learnRevoked(m.rv ?? [])
        if (this.storeInfo?.registered && this.refusal === 'needs-code') this.refusal = null
        // the server dropped its copy (idle eviction): everything up to `floor` is gone; or its log
        // isn't the one this copy last saw (it was put back from a backup)
        const lost = m.mode !== 'relay' && (m.head < this.since || (this.since > 0 && this.since <= (m.floor ?? 0)) || m.lost === true)
        if (lost) { this.seen.clear(); this.last = null; await idbPut(this.db, 'kv', 'last', null) } // numbered afresh; `since` moves back once every page is in
        await this.apply(m.entries)
        if (this.resync === 1) { await this.catchUpAgain(); break }
        // a long log comes in pages ("pages" in the hello): the rest follow as `logmore`
        if (m.more) { this.logPending = { head: m.head, count: m.count, lost }; break }
        await this.onCaughtUp(m.head, m.count, lost)
        break
      case 'logmore': {
        const pending = this.logPending
        if (!pending) break
        await this.apply(m.entries)
        if (this.resync === 1) { this.logPending = null; await this.catchUpAgain(); break }
        if (m.more) break
        this.logPending = null
        await this.onCaughtUp(pending.head, pending.count, pending.lost)
        break
      }
      case 'up':
        await this.apply([m])
        if (this.resync === 1) { await this.catchUpAgain(); break }
        if (m.q) this.markSeen(m.q)
        this.holdForMissing()
        break
      case 'snapped': // the server took our snapshot: what it replaced can go
        if (this.snapRef && this.snapRef.upto === m.upto) {
          const { upto, ref } = this.snapRef
          this.snapRef = null
          if (ref) this.refs[upto] = ref
          await this.dropRefs(upto, ref)
        }
        break
      case 'ack': {
        const ref = this.refSent.get(m.id)
        this.refSent.delete(m.id)
        const h = this.sentHash.get(m.id)
        this.sentHash.delete(m.id)
        if (m.q && h) await this.noteLast(m.q, h)
        if (m.q && ref) { this.refs[m.q] = ref; await idbPut(this.db, 'kv', 'refs', this.refs) }
        if (m.q) this.markSeen(m.q)
        if (m.q && this.refusal) { this.refusal = null; this.onStatus() }
        for (const id of this.inflight.get(m.id) ?? []) await idbDel(this.db, 'outbox', id)
        this.inflight.delete(m.id)
        this.pending = (await idbKeys(this.db, 'outbox')).length
        this.onStatus()
        break
      }
      case 'aw': // one at a time: a "left" must not overtake the last update before it, still being decrypted
        this.presenceQueue = this.presenceQueue.then(() => this.applyPresence(m)).catch((e) => console.error(e))
        break
      case 'left':
        this.presenceQueue = this.presenceQueue.then(() => this.forgetConnection(m.from)).catch((e) => console.error(e))
        break
      case 'p2p':
        await this.onP2p(m)
        break
      case 'rv':
        this.learnRevoked([m])
        break
      case 'deleted': { // the owner deleted the project for everyone: only if the owner signed it
        if (!Number.isInteger(m.ts) || !this.keys.signedByOwner(enc.encode(`delete:${m.ts}`), m.s)) { console.warn('ignored a deletion notice without the owner’s signature'); break }
        this.deletedAt = Number(m.at) || Date.now()
        await idbPut(this.db, 'kv', 'deleted', this.deletedAt)
        this.deleteAck?.(null)
        this.onDeleted()
        this.onStatus()
        break
      }
      case 'usage': // the answer to askUsage(), or news that the project moved to another code
        this.storeInfo = m.store ?? this.storeInfo
        this.onUsage()
        if (m.moved && (this.refusal === 'quota-full' || this.refusal === 'code-expired' || this.refusal === 'project-full')) this.retryStore()
        break
      case 'moved': // moveToCode() done
        this.moveAck?.(null)
        break
      case 'mode':
        this.heard(m.m)
        if (m.m === 'relay') { this.refusal = null; this.modeAck?.(null) } // not storing is the point
        this.onStatus()
        break
      case 'err':
        if (typeof m.code === 'string' && m.code.startsWith('move-') && this.moveAck) this.moveAck(m)
        else if (m.denied) {
          this.denied = m.code
          this.minVersion = typeof m.min === 'string' ? m.min : ''
          this.onStatus()
        } else if (m.code === 'needs-code' && this.storage === 'relay') {
          // sent before the switch to sync-only: it's sent again (as in texloom/replica.py)
        } else if (STORE_PROBLEMS.has(m.code)) {
          // Not stored, but already relayed to anyone online. Keep it in the outbox; retry later.
          if (m.id) this.inflight.delete(m.id)
          this.refusal = m.code
          this.scheduleRetry()
          this.onStatus()
        } else {
          if (m.code === 'mode-rejected') this.modeAck?.(t('the server refused the change'))
          if (m.code === 'delete-rejected') this.deleteAck?.(t('the server refused to delete it'))
          if (m.code === 'mode-busy') this.modeAck?.(t('the server is busy: try again later'))
          console.warn('server:', m.m)
        }
    }
  }

  private retryTimer: ReturnType<typeof setTimeout> | null = null
  private scheduleRetry() {
    if (this.retryTimer || this.refusal === 'needs-code') return // a code only helps once entered
    this.retryTimer = setTimeout(() => { this.retryTimer = null; if (this.status === 'online') this.flushOutbox() }, 5 * 60_000)
  }

  /** Reconnect (e.g. after entering a creation code) so the server re-evaluates storing. */
  retryStore() {
    this.refusal = null
    this.onStatus()
    if (this.syncMode === 'manual') { this.syncNow(); return }
    ;(this.ws as WebSocket | null)?.close() // onclose reconnects; hello carries the new code
  }

  private async noteLast(q: number, h: string) {
    if (!Number.isInteger(q) || q <= (this.last?.[0] ?? 0)) return
    this.last = [q, h]
    await idbPut(this.db, 'kv', 'last', this.last)
  }

  private markSeen(q: number) {
    if (q > this.since) this.seen.add(q)
    while (this.seen.has(this.since + 1) && !this.missing.has(this.since + 1)) this.seen.delete(++this.since)
    idbPut(this.db, 'kv', 'since', this.since)
  }

  private async apply(entries: { q: number; d: string; s: string; snap?: boolean }[]) {
    // One bad entry is skipped, as in texloom/replica.py: stopping there would stop every catch-up at it.
    for (const e of entries) {
      let data: Uint8Array, sig: Uint8Array
      try { data = b64d(e.d); sig = b64d(e.s) } catch { console.warn('dropping a malformed update'); continue }
      if (!this.keys.verify(e.snap ? `snap:${e.q}:` : 'upd:', data, sig)) {
        console.warn('dropping update with bad signature', e.q); continue
      }
      if (!e.snap) await this.noteLast(e.q, await sha(data))
      let update: Uint8Array
      try { update = await this.keys.decrypt(data) } catch (err) { console.warn('dropping undecryptable update', e.q, err); continue }
      if (isRef(update)) {
        const got = await this.fetchRef(update)
        if (got === 'retry') { console.warn('couldn’t fetch the upload change', e.q, 'refers to; trying again shortly'); this.missing.add(e.q); continue }
        if (got === 'gone') {
          console.warn(this.resync ? 'skipping change' : 'the upload change', e.q, this.resync ? ': its upload is gone' : 'refers to is gone: catching up from the start')
          this.resync ||= 1; continue
        }
        this.refs[e.q] = got.id
        await idbPut(this.db, 'kv', 'refs', this.refs)
        update = got.content
      }
      update = await unpack(update) // a compressed snapshot or change (packing.ts)
      try {
        Y.applyUpdate(this.doc, update, REMOTE)
      } catch (err) {
        console.warn('dropping unreadable update', e.q, err)
      }
    }
  }

  private async send(id: string, update: Uint8Array, covers: string[]) {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN || this.status !== 'online') return
    update = await packed(update) // a big change goes compressed (packing.ts)
    if (update.length > UPDATE_BATCH) {
      if (this.serverMode === 'relay' || this.storage === 'relay') {
        // nothing is stored: give it to the people online, in pieces
        await this.p2p({ k: 'upd', u: b64e(update) })
        for (const o of covers) await idbDel(this.db, 'outbox', o)
        this.pending = (await idbKeys(this.db, 'outbox')).length; this.onStatus()
        return
      }
      const ref = await this.asUpload(update)
      if (!ref) return // not uploaded yet: stays in the outbox, and the status says why
      this.refSent.set(id, ref)
      update = refTo(ref)
    }
    const d = await this.keys.encrypt(update)
    this.inflight.set(id, covers)
    this.sentHash.set(id, await sha(d))
    this.ws.send(JSON.stringify({ t: 'up', id, d: b64e(d), s: b64e(this.keys.sign('upd:', d)) }))
  }

  private async flushOutbox() {
    if (!this.keys.writable) return
    const ids = (await idbKeys(this.db, 'outbox')) as string[]
    if (!ids.length) return
    const updates = await Promise.all(ids.map((id) => idbGet<Uint8Array>(this.db, 'outbox', id)))
    // as few messages as fit the server's limit: one for a big paste or upload could be too big
    type Batch = { ids: string[]; updates: Uint8Array[]; size: number }
    const batches: Batch[] = []
    ids.forEach((id, i) => {
      const u = updates[i]
      if (!u) return
      let b = batches[batches.length - 1]
      if (!b || b.size + u.length > UPDATE_BATCH) batches.push(b = { ids: [], updates: [], size: 0 })
      b.ids.push(id); b.updates.push(u); b.size += u.length
    })
    for (const b of batches) await this.send(crypto.randomUUID(), b.updates.length > 1 ? Y.mergeUpdates(b.updates) : b.updates[0], b.ids)
  }

  /** Upload a big change or snapshot like a file; its id, or null if it isn't on the server yet. */
  private async asUpload(content: Uint8Array): Promise<string | null> {
    const id = await this.putBlob(content, 'ref')
    if (await idbGet(this.db, 'pending', id)) return null
    await idbDel(this.db, 'blobs', id) // the server has it: no need for a copy here
    return id
  }

  // ---- uploads on the server (files, and changes too big to send as they are) ----
  private blobUrl(id: string) { return `${this.server}/blob/${this.keys.project}/${id}` }
  /** The upload `id` from the server: null if it couldn't be reached. */
  private fetchBlob(id: string) { return fetch(this.blobUrl(id)).catch(() => null) }
  /** Decrypted, if it is what `id` names (the id is an HMAC of the content: the server can't
   *  substitute another file); else null. Throws if it doesn't decrypt. */
  private async verified(id: string, data: Uint8Array): Promise<Uint8Array | null> {
    const plain = await this.keys.decrypt(data)
    return (await this.keys.blobId(plain)) === id ? plain : null
  }
  /** Delete the upload `id` from the server (`onlyOutput`: unless it's a project file); null if it couldn't be reached. */
  private deleteBlob(id: string, onlyOutput = false) {
    return fetch(this.blobUrl(id), {
      method: 'DELETE', headers: { 'X-Sig': b64e(this.keys.sign(`del:${id}`, new Uint8Array())), ...(onlyOutput ? { 'X-Only-Output': '1' } : {}) },
    }).catch(() => null)
  }

  /** The upload a change refers to: 'retry' if it couldn't be fetched just now, 'gone' if it isn't
   *  there (or isn't what the change names). */
  private async fetchRef(ref: Uint8Array): Promise<{ id: string; content: Uint8Array } | 'retry' | 'gone'> {
    let id: unknown
    try { id = JSON.parse(dec.decode(ref.subarray(REF.length))).b } catch { return 'gone' }
    if (typeof id !== 'string' || !BLOB_ID.test(id)) return 'gone'
    const r = await this.fetchBlob(id)
    if (!r) return 'retry'
    if (r.status === 404 || r.status === 410) return 'gone'
    if (!r.ok) return 'retry'
    let data: Uint8Array
    try { data = new Uint8Array(await r.arrayBuffer()) } catch { return 'retry' }
    try {
      const content = await this.verified(id, data)
      return content ? { id, content } : 'gone'
    } catch { return 'gone' }
  }

  /** `since` stops below a change whose upload couldn't be fetched; reconnecting (after a while,
   *  longer each time) catches up from there. */
  private holdForMissing() {
    if (!this.missing.size) { this.missingTries = 0; return }
    this.since = Math.min(this.since, Math.min(...this.missing) - 1)
    idbPut(this.db, 'kv', 'since', this.since)
    if (this.missingRetry) return
    const wait = Math.min(5000 * 2 ** this.missingTries++, 5 * 60_000)
    this.missingRetry = setTimeout(() => { this.missingRetry = null; this.ws?.close() }, wait) // onclose reconnects
  }
  private missingRetry: ReturnType<typeof setTimeout> | null = null

  /** After a snapshot up to `upto`: the uploads of what it replaced aren't needed any more. */
  private async dropRefs(upto: number, keep: string | null) {
    for (const [q, id] of Object.entries(this.refs)) {
      if (Number(q) > upto || id === keep) continue
      delete this.refs[Number(q)]
      await this.deleteBlob(id, true)
    }
    await idbPut(this.db, 'kv', 'refs', this.refs)
  }

  /** A change refers to an upload that's gone (a snapshot replaced it meanwhile): catch up from
   *  the start, which brings the snapshot instead. Once per session. */
  private async catchUpAgain() {
    if (this.resync !== 1) return
    this.resync = 2
    this.since = 0; this.seen.clear()
    await idbPut(this.db, 'kv', 'since', 0)
    this.ws?.close() // onclose reconnects
  }

  private async compact() {
    if (!this.keys.writable || !this.ws || this.serverMode === 'relay' || this.missing.size) return // (a snapshot would leave a missing change out)
    // what the state covers: read together with it (a change applied while packing or encrypting
    // waits would otherwise count as covered, and leave the log without being in the snapshot)
    const upto = this.since
    const full = await packed(Y.encodeStateAsUpdate(this.doc)) // compressed, so more fits in a message
    let d = await this.keys.encrypt(full), ref: string | null = null
    if (d.length > MAX_SNAPSHOT) { // too big for one message: as an upload
      ref = await this.asUpload(full)
      if (!ref || !this.ws) return // not uploaded: the log stays as it is for now
      d = await this.keys.encrypt(refTo(ref))
    }
    this.snapRef = { upto, ref }
    this.ws.send(JSON.stringify({ t: 'snap', upto, d: b64e(d), s: b64e(this.keys.sign(`snap:${upto}:`, d)) }))
  }

  private async applyPresence(m: { d: string; from?: number }) {
    const upd = await this.keys.decrypt(b64d(m.d)).catch(() => null)
    if (upd) {
      this.presenceSender = m.from
      try { awarenessProtocol.applyAwarenessUpdate(this.awareness, upd, REMOTE) } catch { return }
    }
    this.peers = Math.max(this.peers, this.awareness.getStates().size - 1)
    this.onStatus()
  }

  /** Someone's connection closed: stop showing them now rather than when their presence times out. */
  private forgetConnection(conn: number) {
    const gone = [...this.presenceConn].filter(([id, c]) => c === conn && id !== this.doc.clientID).map(([id]) => id)
    if (!gone.length) return
    awarenessProtocol.removeAwarenessStates(this.awareness, gone, REMOTE)
    for (const id of gone) this.awareness.meta.delete(id) // removal raises their clock: if the page reconnects, it must still be heard
  }

  private async sendAwareness(update: Uint8Array) {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return
    this.ws.send(JSON.stringify({ t: 'aw', d: b64e(await this.keys.encrypt(update)) }))
  }

  // ---- peer-to-peer (relayed, never stored) ------------------------------------
  private async p2p(payload: Record<string, unknown>, to?: number) {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return
    const data = await this.keys.encrypt(enc.encode(JSON.stringify(payload)))
    const d = b64e(data)
    // signed: changes, and compile requests (only writers may ask another computer to compile)
    const s = SIGNED_P2P.has(String(payload.k)) && this.keys.writable ? b64e(this.keys.sign('p2p:', data)) : undefined
    if (d.length <= P2P_PIECE) { await this.sendP2p(d, s, to); return }
    const id = randomHex(8)
    const n = Math.ceil(d.length / P2P_PIECE)
    for (let i = 0; i < n; i++) {
      const part: Record<string, unknown> = { k: 'part', id, i, n, c: d.slice(i * P2P_PIECE, (i + 1) * P2P_PIECE) }
      if (s && i === n - 1) part.s = s // checked once the message is whole
      await this.sendP2p(b64e(await this.keys.encrypt(enc.encode(JSON.stringify(part)))), undefined, to)
    }
  }

  private p2pWindow = { start: 0, bytes: 0 } // see P2P_RATE
  private partAt = 0 // when a piece last came in

  private async sendP2p(d: string, s: string | undefined, to?: number) {
    const now = Date.now()
    if (now - this.p2pWindow.start > P2P_WINDOW) this.p2pWindow = { start: now, bytes: 0 }
    if (this.p2pWindow.bytes + d.length > P2P_RATE) {
      await sleep(Math.max(0, P2P_WINDOW - (now - this.p2pWindow.start)))
      this.p2pWindow = { start: Date.now(), bytes: 0 }
    }
    this.p2pWindow.bytes += d.length
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return
    const msg: any = { t: 'p2p', d }
    if (s) msg.s = s
    if (to !== undefined) msg.to = to
    this.ws.send(JSON.stringify(msg))
  }

  /** Big peer-to-peer messages coming in pieces, by sender and id. */
  private parts = new Map<string, { from: number; n: number; got: Map<number, string>; s?: string; at: number; size: number }>()

  private async onPart(from: number, p: any) {
    const { id, i, n, c } = p
    if (typeof id !== 'string' || !Number.isInteger(i) || !Number.isInteger(n) || typeof c !== 'string'
      || i < 0 || i >= n || n > P2P_PARTS || c.length > P2P_PIECE) return
    const key = `${from}:${id}`, now = Date.now()
    for (const [k, w] of this.parts) if (now - w.at > P2P_STALE) this.parts.delete(k)
    if (!this.parts.has(key)) {
      for (;;) { // the oldest goes: this sender's, else anyone's
        const mine = [...this.parts].filter(([, w]) => w.from === from).map(([k]) => k)
        if (mine.length >= P2P_PER_SENDER) this.parts.delete(mine[0])
        else if (this.parts.size >= P2P_ASSEMBLING) this.parts.delete(this.parts.keys().next().value!)
        else break
      }
      this.parts.set(key, { from, n, got: new Map(), at: now, size: 0 })
    }
    const whole = this.parts.get(key)!
    if (whole.n !== n || whole.got.has(i)) return
    const held = () => [...this.parts.values()].reduce((t, w) => t + w.size, 0)
    while (held() + c.length > P2P_HELD) {
      const old = [...this.parts.keys()].find((k) => k !== key) ?? key
      this.parts.delete(old)
      if (old === key) return // this one alone is more than may be held
    }
    whole.got.set(i, c)
    whole.size += c.length
    whole.at = this.partAt = now
    if (typeof p.s === 'string') whole.s = p.s
    if (whole.got.size === n) {
      this.parts.delete(key)
      await this.onP2p({ d: Array.from({ length: n }, (_, j) => whole.got.get(j)!).join(''), s: whole.s, from })
    }
  }

  // Peers ask for what they lack (sv1/sv2: all of the document they don't have; breq: a file).
  // Anyone with the link can ask, a read-only one too, and a few bytes can ask for megabytes:
  // each connection gets at most one answer of each kind per ANSWER_EVERY (a state vector asked
  // for sooner is answered then, with the latest), and answers go out one at a time.
  private answered = new Map<string, number>() // what (connection, or connection:file) -> when
  private stateWanted = new Map<number, string>() // connection -> its latest state vector, to answer
  private answers: Promise<void> = Promise.resolve()

  private mayAnswer(key: string): boolean {
    const now = Date.now()
    if (this.answered.size > 1000) for (const [k, at] of this.answered) if (now - at > ANSWER_EVERY) this.answered.delete(k)
    if (now - (this.answered.get(key) ?? 0) < ANSWER_EVERY) return false
    this.answered.set(key, now)
    return true
  }

  private answer(fn: () => Promise<void>) {
    this.answers = this.answers.then(fn).catch((e) => console.warn('couldn’t answer a peer', e))
  }

  private answerState(from: number, sv: string) {
    const waiting = this.stateWanted.has(from)
    this.stateWanted.set(from, sv)
    if (waiting) return
    const wait = Math.max(0, (this.answered.get(`${from}`) ?? 0) + ANSWER_EVERY - Date.now())
    setTimeout(() => this.answer(async () => {
      const want = this.stateWanted.get(from)
      this.stateWanted.delete(from)
      if (want === undefined || !this.mayAnswer(`${from}`)) return
      const diff = Y.encodeStateAsUpdate(this.doc, b64d(want))
      if (diff.length > 2) await this.p2p({ k: 'upd', u: b64e(await packed(diff)) }, from)
    }), wait)
  }

  private async onP2p(m: { d: string; s?: string; from: number }) {
    const data = b64d(m.d)
    let p: any
    try { p = JSON.parse(dec.decode(await this.keys.decrypt(data))) } catch { return }
    switch (p.k) {
      case 'part':
        await this.onPart(m.from, p)
        break
      case 'fold': // the app on another computer syncs a folder of this project
        if (typeof p.f === 'string' && /^[0-9a-f]{32}$/.test(p.f)) this.folders.set(p.f, { at: Date.now(), holding: !!p.h, by: String(p.by ?? '').slice(0, 60) })
        break
      case 'sv1': // a peer just connected: send what it lacks, and ask for what we lack
      case 'sv2': {
        this.peers = Math.max(this.peers, 1); this.onStatus()
        if (this.keys.writable && typeof p.sv === 'string') this.answerState(m.from, p.sv)
        if (p.k === 'sv1') await this.p2p({ k: 'sv2', sv: b64e(Y.encodeStateVector(this.doc)) }, m.from)
        break
      }
      case 'upd':
        // Not before the server's list of revocations is in: a revoked link could slip one in.
        // (Peers resend after our state vector, which goes out once the log is applied.)
        if (!this.synced) return
        if (!m.s || !this.keys.verify('p2p:', data, b64d(m.s), true)) { console.warn('unsigned p2p update ignored'); return }
        Y.applyUpdate(this.doc, await unpack(b64d(p.u)), REMOTE)
        break
      case 'breq': {
        if (typeof p.id !== 'string' || !BLOB_ID.test(p.id) || !this.mayAnswer(`${m.from}:${p.id}`)) return
        const id = p.id, to = m.from
        this.answer(async () => {
          const blob = await idbGet<Uint8Array>(this.db, 'blobs', id)
          if (blob && blob.length <= P2P_BLOB) await this.p2p({ k: 'blob', id, b: b64e(blob) }, to)
        })
        break
      }
      case 'cack': // a texloom helper accepted our compile request
      case 'cerr': { // it couldn't compile (no TeX on that computer, or the compile stopped)
        // Only from a writer (signed: a read-only member can't fake one) answering one of our own
        // requests (its nonce: an old answer replayed doesn't count).
        if (typeof p.n !== 'string' || !this.compileNonces.includes(p.n)) return
        if (!m.s || !this.keys.verify('p2p:', data, b64d(m.s), true)) { console.warn('unsigned compile answer ignored'); return }
        const by = String(p.by ?? '').slice(0, 100)
        if (p.k === 'cack') this.onCompileAck({ by })
        else if (typeof p.why === 'string') this.onCompileError({ by, why: p.why.slice(0, 500) })
        break
      }
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

  /** Ask connected computers that compile this project (the TeXLoom app, texloom run --compile) to compile now; `fromScratch`
   *  deletes their build folder (.aux, .bbl, …) first. */
  async requestCompile(fromScratch = false): Promise<boolean> {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return false
    const n = b64e(crypto.getRandomValues(new Uint8Array(12)))
    this.compileNonces = [...this.compileNonces.slice(-9), n] // a helper may answer a recent one later
    await this.p2p(fromScratch ? { k: 'creq', clean: true, n } : { k: 'creq', n })
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
        if (!this.keys.signedByOwner(revokeMsg(pub), it.s)) continue
        this.keys.revoked.add(pub)
        this.revocations.push({ p: pub, s: it.s })
        added = true
        if (pub === this.keys.memberPub && !this.withdrawn) {
          this.withdrawn = true
          local.set(`texloom.withdrawn.${this.keys.project}`, '1')
          this.onStatus()
        }
      } catch (e) { quiet('a malformed or unverifiable revocation: skipped', e) }
    }
    if (added && save) idbPut(this.db, 'kv', 'revoked', this.revocations).catch((e) => quiet('revocations not saved here: they come again with the document', e))
  }
  private revocations: { p: string; s: string }[] = []

  /** Owner only: withdraw a personal edit link. */
  revoke(pub: string): boolean {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return false
    const rv = this.keys.revocation(pub)
    this.ws.send(JSON.stringify({ t: 'revoke', ...rv }))
    this.revokedDoc.set(rv.p, rv.s) // in the document too, for anyone the server doesn't tell
    return true
  }

  private async sendMode(mode: StorageMode) {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return
    const ts = Math.floor(Date.now() / 1000)
    this.ws.send(JSON.stringify({ t: 'mode', m: mode, ts, s: b64e(this.keys.sign(`mode:${mode}:${ts}`, new Uint8Array())) }))
    this.serverMode = mode
    if (mode === 'relay') this.refusal = null
    await idbDel(this.db, 'kv', 'pendingMode')
    if (mode === 'server') {
      // Give the server a complete copy again: full state plus every blob we hold.
      await this.send(crypto.randomUUID(), Y.encodeStateAsUpdate(this.doc), [])
      for (const id of (await idbKeys(this.db, 'blobs')) as string[]) await idbPut(this.db, 'pending', id, 1)
      await this.uploadPending()
    }
    this.onStatus()
  }

  private modeAck: ((error: string | null) => void) | null = null

  /** Wait for the server's log to have been applied; false if that didn't happen in time. */
  async caughtUp(timeoutMs: number): Promise<boolean> {
    const until = Date.now() + timeoutMs
    while (!(this.status === 'online' && this.synced)) {
      if (Date.now() > until) return false
      await sleep(200)
    }
    return true
  }

  /** Owner: the server deletes its encrypted copy and stops storing this project. Resolves
   *  once the server confirms (connecting first if needed), or rejects with its reason. */
  async removeFromServer(timeoutMs = 20000): Promise<void> {
    if (!this.keys.owner) throw new Error(t('only the owner can remove the server’s copy'))
    if (!this.ws) this.connect()
    const until = Date.now() + timeoutMs
    if (!await this.caughtUp(timeoutMs)) throw new Error(t('the server can’t be reached'))
    const done = new Promise<void>((resolve, reject) => {
      const timer = setTimeout(() => { this.modeAck = null; reject(new Error(t('the server didn’t confirm'))) }, Math.max(1000, until - Date.now()))
      this.modeAck = (error) => { clearTimeout(timer); this.modeAck = null; error ? reject(new Error(error)) : resolve() }
    })
    await this.setStorage('relay')
    await done
  }

  private deleteAck: ((error: string | null) => void) | null = null
  private moveAck: ((refusal: MoveRefusal | null) => void) | null = null

  /** Owner: move the stored project to another creation code (issue #32). Resolves null once the
   *  server has moved it, else with its refusal; rejects if the server can't be reached. */
  async moveToCode(code: string, timeoutMs = 20000): Promise<MoveRefusal | null> {
    if (!this.keys.owner) throw new Error(t('only the owner can move the project to another code'))
    const ws = this.ws
    if (!ws || ws.readyState !== WebSocket.OPEN) throw new Error(t('the server can’t be reached'))
    code = code.trim()
    const done = new Promise<MoveRefusal | null>((resolve, reject) => {
      const timer = setTimeout(() => { this.moveAck = null; reject(new Error(t('the server didn’t confirm'))) }, timeoutMs)
      this.moveAck = (refusal) => { clearTimeout(timer); this.moveAck = null; resolve(refusal) }
    })
    const ts = Math.floor(Date.now() / 1000)
    ws.send(JSON.stringify({ t: 'move', c: code, ts, s: b64e(this.keys.sign(`move:${code}:${ts}`, new Uint8Array())) }))
    return done
  }

  /** Owner: delete the project for everyone. The server erases its copy and from then on refuses
   *  the project to anyone, so collaborators' apps keep their copy read-only. Resolves once the
   *  server confirms (connecting first if needed), or rejects with its reason. */
  async deleteForEveryone(timeoutMs = 20000): Promise<void> {
    if (!this.keys.owner) throw new Error(t('only the owner can delete the project for everyone'))
    if (this.deletedAt) return
    if (!this.ws) this.connect()
    const until = Date.now() + timeoutMs
    while (!(this.ws && this.ws.readyState === WebSocket.OPEN)) {
      if (this.deletedAt) return
      if (Date.now() > until) throw new Error(t('the server can’t be reached'))
      await sleep(200)
    }
    const done = new Promise<void>((resolve, reject) => {
      const timer = setTimeout(() => { this.deleteAck = null; reject(new Error(t('the server didn’t confirm'))) }, Math.max(1000, until - Date.now()))
      this.deleteAck = (error) => { clearTimeout(timer); this.deleteAck = null; error ? reject(new Error(error)) : resolve() }
    })
    const ts = Math.floor(Date.now() / 1000)
    this.ws.send(JSON.stringify({ t: 'delete', ts, s: b64e(this.keys.sign(`delete:${ts}`, new Uint8Array())) }))
    await done
  }

  private signStorage(mode: StorageMode) {
    const at = Date.now()
    this.meta.set('storage', { m: mode, at, s: this.keys.ownerSign(`storage:${mode}:${at}`) })
  }

  /** Change where the project lives (the owner only). 'relay' deletes the server's copy. */
  async setStorage(mode: StorageMode) {
    if (!this.keys.owner) throw new Error(t('Only the owner can change where the project is kept.'))
    this.signStorage(mode)
    await idbPut(this.db, 'kv', 'pendingMode', mode)
    await this.sendMode(mode)
  }

  // ---- blobs ---------------------------------------------------------------
  /** Store file content here and upload it. 'output' marks compiled PDFs, logs and SyncTeX
   *  data: replaced by the next compile, so collaborators may delete them from the server. */
  async putBlob(plain: Uint8Array, kind: 'file' | 'output' | 'ref' = 'file'): Promise<string> {
    const id = await this.keys.blobId(plain)
    const cached = !!(await idbGet(this.db, 'blobs', id))
    if (!cached) await idbPut(this.db, 'blobs', id, plain)
    // a project file is (re)uploaded even if cached, in case the server dropped it as output
    if (!cached || kind === 'file') await idbPut(this.db, 'pending', id, kind === 'file' ? 1 : 'output')
    if (this.status === 'online') await this.uploadPending()
    return id
  }

  /** Ids ever used as project files, and ever published as compiled output. The document
   *  keeps every past value (gc is off), so this covers what History could restore. */
  private historyIds(): { files: Set<string>; outputs: Set<string> } {
    const walk = (m: Y.Map<any>, pick: (v: any) => unknown[]) => {
      const out = new Set<string>()
      for (const last of (m as any)._map.values() as Iterable<any>)
        for (let it = last; it; it = it.left)
          for (const v of it.content?.getContent?.() ?? []) for (const id of pick(v)) if (typeof id === 'string' && BLOB_ID.test(id)) out.add(id)
      return out
    }
    return { files: walk(this.bin, (v) => [v?.h]), outputs: walk(this.out, (v) => [v?.pdf, v?.log, v?.synctex]) }
  }

  /** Remove compiled output that a newer compile replaced, here and (for writers) on the server.
   *  Never touches anything that was ever a project file. */
  async pruneOutputs() {
    const { files, outputs } = this.historyIds()
    const cur = this.out.get('latest') ?? {}
    const keep = new Set([cur.pdf, cur.log, cur.synctex])
    const done = new Set((await idbGet<string[]>(this.db, 'kv', 'dropped')) ?? [])
    const old = [...outputs].filter((id) => !keep.has(id) && !files.has(id) && !done.has(id))
    if (!old.length) return
    const toServer = this.keys.writable && this.serverMode === 'server' && this.storage === 'server' && this.status === 'online'
    for (const id of old) {
      await idbDel(this.db, 'blobs', id); await idbDel(this.db, 'pending', id)
      if (toServer) {
        if (!(await this.deleteBlob(id))) continue // offline: try again later
      }
      done.add(id) // deleted, gone already, or kept for the owner's browser to remove
    }
    if (!toServer) for (const id of old) done.delete(id) // only local so far: the server copy is someone else's to drop
    await idbPut(this.db, 'kv', 'dropped', [...done])
  }

  /** Give the server back its copy: the whole document, and every file we have. */
  private async reseed() {
    console.info('the server has no copy of this project any more: re-uploading it')
    for (const [, b] of this.bin) if (b?.h && (await idbGet(this.db, 'blobs', b.h))) await idbPut(this.db, 'pending', b.h, 1)
    // In the outbox, as in texloom/replica.py: sent with the rest once online (onCaughtUp uploads the
    // files first), and again on reconnecting until the server has it. (Sent from here, before the
    // connection counted as online, it was dropped.)
    await idbPut(this.db, 'outbox', crypto.randomUUID(), Y.encodeStateAsUpdate(this.doc))
    this.pending = (await idbKeys(this.db, 'outbox')).length
  }

  /** Uploads run one at a time: overlapping runs would send the same item twice, the second
   *  time without its "output" mark. */
  private uploadChain: Promise<void> = Promise.resolve()
  private uploadPending(): Promise<void> {
    const run = this.uploadChain.then(() => this.uploadPendingNow())
    this.uploadChain = run.catch((e) => quiet('the upload reports its own failure; the chain only waits for it', e))
    return run
  }

  private async uploadPendingNow() {
    if (!this.keys.writable || this.serverMode === 'relay' || this.storage === 'relay') return
    let hist: ReturnType<Replica['historyIds']> | null = null
    for (const id of (await idbKeys(this.db, 'pending')) as string[]) {
      const plain = await idbGet<Uint8Array>(this.db, 'blobs', id)
      if (!plain) { await idbDel(this.db, 'pending', id); continue }
      const marked = (await idbGet(this.db, 'pending', id)) === 'output'
      hist ??= this.historyIds()
      const output = marked || (hist.outputs.has(id) && !hist.files.has(id))
      const d = await this.keys.encrypt(plain)
      const code = this.creationCode()
      const r = await fetch(this.blobUrl(id), {
        method: 'PUT', body: d as BodyInit,
        headers: { 'X-Sig': b64e(this.keys.sign(`blob:${id}:`, d)), ...(code ? { 'X-Create-Code': code } : {}), ...(output ? { 'X-Kind': 'output' } : {}) },
      }).catch(() => null)
      if (r && [403, 413, 507].includes(r.status)) {
        const why = (await r.json().catch(() => ({}))).code
        if (STORE_PROBLEMS.has(why)) { this.refusal = why; this.scheduleRetry(); this.onStatus() }
      }
      if (!r?.ok) return
      await idbDel(this.db, 'pending', id)
      if (this.refusal) { this.refusal = null; this.onStatus() } // stored again (as in texloom/replica.py)
    }
  }

  async getBlob(id: string): Promise<Uint8Array> {
    if (!BLOB_ID.test(id)) throw new Error(t('not a file id')) // ids come from the shared document
    const cached = await idbGet<Uint8Array>(this.db, 'blobs', id)
    if (cached) return cached
    if (this.serverMode === 'server') {
      const r = await this.fetchBlob(id)
      if (r?.ok) {
        const plain = await this.verified(id, new Uint8Array(await r.arrayBuffer()))
        if (plain) {
          await idbPut(this.db, 'blobs', id, plain)
          return plain
        }
        console.warn('server returned the wrong content for', id)
      }
    }
    // Ask connected peers for it.
    const got = new Promise<Uint8Array>((res) => this.blobWaiters.set(id, [...(this.blobWaiters.get(id) ?? []), res]))
    await this.p2p({ k: 'breq', id })
    const asked = Date.now()
    let r: Uint8Array | null = null
    while (!r) { // a big file comes in pieces: wait while they keep coming
      r = await Promise.race([got, sleep(5000).then(() => null)])
      if (!r && Date.now() - Math.max(asked, this.partAt) > 20000) break
    }
    if (!r) throw new Error(t('file content not available yet ({{id}}): no online peer has it', { id: id.slice(0, 8) }))
    return r
  }

  /** All project files as {path: content}, for compiling or exporting. */
  async snapshotFiles(): Promise<Map<string, string | Uint8Array>> {
    const out = new Map<string, string | Uint8Array>()
    this.files.forEach((t, p) => out.set(p, t.toString()))
    // all at once: one that has to be asked of other computers can take up to 20 seconds
    const bins = [...this.bin.entries()]
    const got = await Promise.all(bins.map(([, b]) => this.getBlob(b.h)))
    bins.forEach(([p], i) => out.set(p, got[i]))
    return out
  }
}

/** A peer's presence state, made harmless: names are text, colours must be colours. */
function cleanPresence(st: any) {
  const u = st?.user
  if (!u || typeof u !== 'object') return
  if (typeof u.name !== 'string') u.name = t('Someone')
  u.name = u.name.slice(0, 60)
  for (const k of ['color', 'colorLight']) if (k in u && (typeof u[k] !== 'string' || !COLOR.test(u[k]))) u[k] = '#888888'
}
