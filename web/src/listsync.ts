// Keep the list of your projects on the server, encrypted, so it follows you to other browsers
// and computers. Must match texloom/listsync.py exactly. The dialogs are in listdialog.ts.
//
// A list is opened by one random secret, carried in a "sync link" (…/#sync=…: like a project
// link, the part after # never reaches the server). From it come an encryption key and a signing
// key whose public half is the list's id, so the server holds something it can neither read nor
// change. Passkeys and passwords are other ways in: each yields a secret that opens a small entry
// of the same kind, holding the list's secret. A further entry, opened by the list's secret,
// records those entries' signing keys, so that they can be removed.
import { ed25519 } from '@noble/curves/ed25519.js'
import { scryptAsync } from '@noble/hashes/scrypt.js'
import { b64d, b64e } from './crypto'
import { projects } from './projects'
import { creationCode } from './replica'

export interface ListEntry { f: string; n: string; at: number; w: boolean; nick?: string; t?: string; by?: string }
export interface ListDoc { v: 1; projects: Record<string, ListEntry>; gone: Record<string, number> }

/** The message is a code: needs-code, code-expired, list-full, list-retired, password-taken,
 *  wrong-password, no-prf, passkey-failed, busy or server. */
export class ListError extends Error {}

const STORE = 'texloom.listKey'
const YEAR = 365 * 86400_000
const PASSKEY_SALT = new TextEncoder().encode('texloom project list v1')
const enc = new TextEncoder(), dec = new TextDecoder()

// ---- keys ----------------------------------------------------------------------------------
const hmac = async (key: Uint8Array, label: string) => new Uint8Array(await crypto.subtle.sign('HMAC',
  await crypto.subtle.importKey('raw', key as BufferSource, { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']), enc.encode(label) as BufferSource))

class ListKey {
  private constructor(readonly secret: Uint8Array, readonly id: string, private seed: Uint8Array, private aes: CryptoKey) {}

  static async of(secret: Uint8Array): Promise<ListKey> {
    const seed = await hmac(secret, 'texloom list sign')
    const aes = await crypto.subtle.importKey('raw', await hmac(secret, 'texloom list encrypt') as BufferSource, 'AES-GCM', false, ['encrypt', 'decrypt'])
    return new ListKey(secret, b64e(ed25519.getPublicKey(seed)), seed, aes)
  }

  async encrypt(plain: Uint8Array): Promise<Uint8Array> {
    const iv = crypto.getRandomValues(new Uint8Array(12))
    const ct = new Uint8Array(await crypto.subtle.encrypt({ name: 'AES-GCM', iv }, this.aes, plain as BufferSource))
    const out = new Uint8Array(12 + ct.length)
    out.set(iv); out.set(ct, 12)
    return out
  }

  async decrypt(data: Uint8Array): Promise<Uint8Array> {
    return new Uint8Array(await crypto.subtle.decrypt({ name: 'AES-GCM', iv: data.subarray(0, 12) as BufferSource }, this.aes, data.subarray(12) as BufferSource))
  }

  get signingSeed(): string { return b64e(this.seed) }

  signPut(seq: number, data: Uint8Array): string {
    const head = enc.encode(`list:${this.id}:${seq}:`)
    const msg = new Uint8Array(head.length + data.length)
    msg.set(head); msg.set(data, head.length)
    return b64e(ed25519.sign(msg, this.seed))
  }
}

export const newSecret = () => crypto.getRandomValues(new Uint8Array(32))
export const linkFor = (secret: Uint8Array) => `${location.origin}/#sync=${b64e(secret)}`

// ---- merging -------------------------------------------------------------------------------
const emptyList = (): ListDoc => ({ v: 1, projects: {}, gone: {} })

/** One spelling of a document, to tell whether two are the same. */
export function canon(x: unknown): string {
  if (Array.isArray(x)) return `[${x.map(canon).join(',')}]`
  if (x && typeof x === 'object') {
    const o = x as Record<string, unknown>
    return `{${Object.keys(o).filter((k) => o[k] !== undefined).sort().map((k) => `${JSON.stringify(k)}:${canon(o[k])}`).join(',')}}`
  }
  return JSON.stringify(x)
}

/** Two lists as one, the same whichever way round. The most recently opened copy of an entry
 *  wins, except that an edit link is never replaced by a read-only one; removed projects stay
 *  removed unless they were opened again afterwards. */
export function mergeLists(a: ListDoc, b: ListDoc): ListDoc {
  const out = emptyList()
  for (const doc of [a, b]) for (const [p, when] of Object.entries(doc.gone ?? {})) out.gone[p] = Math.max(out.gone[p] ?? 0, when)
  for (const p of new Set([...Object.keys(a.projects ?? {}), ...Object.keys(b.projects ?? {})])) {
    const x = a.projects?.[p], y = b.projects?.[p]
    let e: ListEntry
    if (x && y) {
      const xFirst = x.at !== y.at ? x.at > y.at : canon(x) >= canon(y)
      const [fresh, old] = xFirst ? [x, y] : [y, x]
      e = { ...old, ...Object.fromEntries(Object.entries(fresh).filter(([, v]) => v !== undefined && v !== '')) } as ListEntry
      if (old.w && !fresh.w) { e.f = old.f; e.w = true }
    } else e = { ...(x ?? y)! }
    if (e.at > (out.gone[p] ?? 0)) out.projects[p] = e
  }
  return out
}

function forgetOldRemovals(doc: ListDoc): ListDoc {
  const cutoff = Date.now() - YEAR
  return { ...doc, gone: Object.fromEntries(Object.entries(doc.gone).filter(([, when]) => when >= cutoff)) }
}

// ---- the server ----------------------------------------------------------------------------
async function pull<T>(key: ListKey): Promise<{ seq: number; doc: T } | null> {
  const r = await fetch(`/api/list/${key.id}`, { cache: 'no-store' })
  if (r.status === 404) return null
  if (r.status === 410) throw new ListError('list-retired')
  if (!r.ok) throw new ListError('server')
  const plain = await key.decrypt(new Uint8Array(await r.arrayBuffer()))
  return { seq: Number(r.headers.get('X-Seq')), doc: JSON.parse(dec.decode(plain)) as T }
}

/** False if someone else got there first. */
async function push(key: ListKey, seq: number, doc: unknown): Promise<boolean> {
  const data = await key.encrypt(enc.encode(canon(doc)))
  const headers: Record<string, string> = { 'X-Seq': String(seq), 'X-Sig': key.signPut(seq, data), 'Content-Type': 'application/octet-stream' }
  const code = creationCode.get()
  if (code) headers['X-Create-Code'] = code
  const r = await fetch(`/api/list/${key.id}`, { method: 'PUT', headers, body: data as BodyInit })
  if (r.status === 409) return false
  if (!r.ok) throw new ListError(await r.json().then((x) => String(x.code ?? 'server'), () => 'server'))
  return true
}

/** Read an entry, change it, and write it back unless that changed nothing, retrying if another
 *  device got there first. */
async function update<T>(key: ListKey, change: (had: T | null) => T): Promise<T> {
  for (let i = 0; i < 5; i++) {
    const had = await pull<T>(key)
    const before = had && canon(had.doc) // `change` may alter what it is given
    const next = change(had?.doc ?? null)
    if (before === canon(next)) return next
    if (await push(key, (had?.seq ?? 0) + 1, next)) return next
  }
  throw new ListError('busy')
}

/** Merge `local` with the server's list and update the server if that changed it. */
const exchange = (key: ListKey, local: ListDoc) => update<ListDoc>(key, (had) => forgetOldRemovals(mergeLists(had ?? emptyList(), local)))

/** Delete an entry, keeping its id out of use, given the seed of its signing key. */
async function retire(id: string, seed: string) {
  const sig = b64e(ed25519.sign(enc.encode(`listdel:${id}`), b64d(seed)))
  const r = await fetch(`/api/list/${id}`, { method: 'DELETE', headers: { 'X-Sig': sig } })
  if (!r.ok) throw new ListError('server')
}

// ---- this browser --------------------------------------------------------------------------
let lastSynced = ''
let lastAttempt = 0
let lastError: string | null = null
let timer: ReturnType<typeof setTimeout> | undefined
let running: Promise<void> | null = null
let changed: () => void = () => {}

export function savedSecret(): Uint8Array | null {
  try {
    const s = localStorage.getItem(STORE)
    return s ? b64d(s) : null
  } catch { return null }
}

/** Use this list on this browser, or none. */
export function saveSecret(secret: Uint8Array | null) {
  try {
    if (secret) localStorage.setItem(STORE, b64e(secret))
    else localStorage.removeItem(STORE)
  } catch {}
  lastSynced = ''
  lastError = null
}

export const lastProblem = () => lastError

export function syncNow(): Promise<void> {
  if (!savedSecret()) return Promise.resolve()
  return (running ??= (async () => {
    try {
      lastAttempt = Date.now()
      const doc = await exchange(await ListKey.of(savedSecret()!), projects.asList())
      const different = projects.applyList(doc)
      lastSynced = canon(projects.asList())
      lastError = null
      if (different) changed()
    } catch (e) {
      lastError = e instanceof ListError ? e.message : 'network'
      throw e
    } finally { running = null }
  })())
}
const syncQuietly = () => { syncNow().catch(() => {}) }

function schedule() {
  if (!savedSecret()) return
  clearTimeout(timer)
  timer = setTimeout(() => { if (canon(projects.asList()) !== lastSynced) syncQuietly() }, 3000)
}

/** Keep this browser's list up to date; `onChange` runs when another device changed it. */
export function startListSync(onChange: () => void) {
  changed = onChange
  projects.onChange(schedule)
  syncQuietly()
  window.addEventListener('focus', () => { if (Date.now() - lastAttempt > 60_000) syncQuietly() })
  window.addEventListener('online', syncQuietly)
}

export async function adopt(secret: Uint8Array) {
  saveSecret(secret)
  await syncNow()
}

// ---- passkeys ------------------------------------------------------------------------------
export const passkeySupported = () => 'credentials' in navigator && 'PublicKeyCredential' in window

const prfRequest = { prf: { eval: { first: PASSKEY_SALT } } } as AuthenticationExtensionsClientInputs
const challenge = () => crypto.getRandomValues(new Uint8Array(32))

function prfOf(cred: PublicKeyCredential): Uint8Array | null {
  const first = (cred.getClientExtensionResults() as { prf?: { results?: { first?: BufferSource } } }).prf?.results?.first
  return first ? new Uint8Array(first as ArrayBuffer) : null
}

/** A browser passkey prompt; null if the person cancelled. */
async function askPasskey(call: () => Promise<Credential | null>): Promise<PublicKeyCredential | null> {
  try { return await call() as PublicKeyCredential | null }
  catch (e) {
    if ((e as Error).name === 'NotAllowedError') return null
    console.warn('passkey:', e)
    throw new ListError('passkey-failed')
  }
}

async function usePasskey(only: BufferSource[] = []): Promise<Uint8Array | null> {
  const cred = await askPasskey(() => navigator.credentials.get({ publicKey: {
    challenge: challenge(),
    userVerification: 'required',
    allowCredentials: only.map((id) => ({ type: 'public-key', id })),
    extensions: prfRequest,
  } }))
  if (!cred) return null
  const out = prfOf(cred)
  if (!out) throw new ListError('no-prf')
  return out
}

/** A new passkey's secret; null if the person cancelled. */
export async function createPasskey(): Promise<Uint8Array | null> {
  const cred = await askPasskey(() => navigator.credentials.create({ publicKey: {
    rp: { name: 'TeXLoom' },
    user: { id: crypto.getRandomValues(new Uint8Array(16)), name: 'TeXLoom project list', displayName: 'TeXLoom project list' },
    challenge: challenge(),
    pubKeyCredParams: [{ type: 'public-key', alg: -7 }, { type: 'public-key', alg: -257 }],
    authenticatorSelection: { residentKey: 'required', userVerification: 'required' },
    extensions: prfRequest,
  } }))
  if (!cred) return null
  const made = prfOf(cred)
  if (made) return made
  if (!(cred.getClientExtensionResults() as { prf?: { enabled?: boolean } }).prf?.enabled) throw new ListError('no-prf')
  return usePasskey([cred.rawId]) // many passkeys only give their secret when used, not when made
}

// ---- other ways in, and removing them -------------------------------------------------------
export interface Slot { kind: 'passkey' | 'password'; at: number; seed: string }
type Registry = { v: 1; slots: Record<string, Slot> }
const noSlots = (): Registry => ({ v: 1, slots: {} })

const registryKey = async (secret: Uint8Array) => ListKey.of(await hmac(secret, 'texloom list slots'))

export async function readSlots(secret: Uint8Array): Promise<Record<string, Slot>> {
  return (await pull<Registry>(await registryKey(secret)))?.doc.slots ?? {}
}

async function changeSlots(secret: Uint8Array, change: (slots: Record<string, Slot>) => void) {
  await update<Registry>(await registryKey(secret), (had) => {
    const doc = had ?? noSlots()
    change(doc.slots)
    return doc
  })
}

/** Make `unlocking` a way into the list; returns its id. */
export async function addSlot(secret: Uint8Array, unlocking: Uint8Array, kind: Slot['kind']): Promise<string> {
  const key = await ListKey.of(unlocking)
  await update<{ v: 1; wrap: string }>(key, (had) => {
    if (had && kind === 'password') throw new ListError('password-taken')
    return { v: 1, wrap: b64e(secret) }
  }).catch((e) => {
    throw e instanceof ListError && e.message === 'list-retired' ? new ListError('password-taken') : e // used before, then changed
  })
  await changeSlots(secret, (slots) => { slots[key.id] = { kind, at: Date.now(), seed: key.signingSeed } })
  return key.id
}

export async function removeSlot(secret: Uint8Array, id: string, slot: Slot) {
  await retire(id, slot.seed)
  await changeSlots(secret, (slots) => { delete slots[id] })
}

/** The list's secret, from a way in; null if that doesn't open anything. */
async function openSlot(unlocking: Uint8Array): Promise<Uint8Array | null> {
  const had = await pull<{ wrap: string }>(await ListKey.of(unlocking)).catch((e) => {
    if (e instanceof ListError && e.message === 'list-retired') return null // removed
    throw e
  })
  return had ? b64d(had.doc.wrap) : null
}

/** The list's secret from a passkey: null if cancelled, 'none' if it opens nothing here. */
export async function unlockWithPasskey(): Promise<Uint8Array | 'none' | null> {
  const unlocking = await usePasskey()
  return unlocking ? (await openSlot(unlocking)) ?? 'none' : null
}

export const MIN_PASSWORD = 12

/** What a username and password stand for. Slow on purpose, and done on this device: neither is
 *  ever sent anywhere. Must match texloom/listsync.py. */
const passwordSecret = (username: string, password: string) => scryptAsync(
  enc.encode(password.normalize('NFKC')),
  enc.encode(`texloom list password v1:${username.trim().toLowerCase().normalize('NFKC')}`),
  { N: 2 ** 17, r: 8, p: 1, dkLen: 32, maxmem: 256 * 1024 * 1024 + 1024 },
)

/** Set the list's one password, replacing any earlier one. */
export async function setPassword(secret: Uint8Array, username: string, password: string) {
  const fresh = await addSlot(secret, await passwordSecret(username, password), 'password')
  for (const [id, slot] of Object.entries(await readSlots(secret))) if (slot.kind === 'password' && id !== fresh) await removeSlot(secret, id, slot)
}

export async function unlockWithPassword(username: string, password: string): Promise<Uint8Array> {
  const secret = await openSlot(await passwordSecret(username, password))
  if (!secret) throw new ListError('wrong-password')
  return secret
}

/** Delete a list and every way into it, keeping their ids out of use. */
async function retireList(secret: Uint8Array) {
  for (const [id, slot] of Object.entries(await readSlots(secret))) await retire(id, slot.seed)
  for (const key of [await registryKey(secret), await ListKey.of(secret)]) await retire(key.id, key.signingSeed)
}

/** Move the list to a new secret, for when the link has got out. Everything that opened the old
 *  one stops working, including passkeys and passwords: whoever has the old link could read them. */
export async function replaceList(secret: Uint8Array) {
  await syncNow()
  const fresh = newSecret()
  await exchange(await ListKey.of(fresh), projects.asList())
  await retireList(secret)
  await adopt(fresh)
}

export async function deleteList(secret: Uint8Array) {
  await retireList(secret)
  saveSecret(null)
}
