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
import { b64d, b64e } from './crypto'
import { canon, emptyList, emptySettings, emptyTags, forgetOldRemovals, hmac, ListKey, mergeLists, mergeSettings, mergeTags, passwordSecret, signPut, tidyTags, type ListDoc, type SettingsDoc, type TagDoc } from './listcore'
import { projects } from './projects'
import { creationCode } from './replica'
import { myName, NAME_CHANGED } from './myname'
import { zoteroAccount } from './zotero'
import { githubAccount, otherAccounts } from './github'
import { dropboxAccount } from './dropbox'
import type { AccountStore } from './accounts'
import { tags } from './tags'
import { normalServer } from './servers'
import { APP_HEADER } from './version'

/** The message is a code: needs-code, code-expired, list-full, list-retired, password-taken,
 *  wrong-password, no-prf, passkey-failed, busy or server. */
export class ListError extends Error {}

const STORE = 'texloom.listKey'
const PASSKEY_SALT = new TextEncoder().encode('texloom project list v1')
const enc = new TextEncoder(), dec = new TextDecoder()

export const newSecret = () => crypto.getRandomValues(new Uint8Array(32))
/** The sync link (it works on any server keeping a copy: change its address to use another). */
export const linkFor = (secret: Uint8Array) => `${copyServers()[0] ?? location.origin}/#sync=${b64e(secret)}`

// ---- the servers keeping copies --------------------------------------------------------------
// A list can be kept on several servers, so it outlives any one of them. Its settings carry
// copy:<origin> (on or off, with when that was set), every device remembers the set, and each sync
// reads every copy, merges, and writes back to those that lack something. Every part of a list
// merges in any order, so a copy that was offline catches up the next time. A list retired on any
// copy (replaced or deleted) is retired. Same rules as texloom/listsync.py.
const COPIES = 'texloom.listCopies'
type Copies = Record<string, { on: boolean; at: number }>
const firstCopy = (server = location.origin): Copies => ({ [server]: { on: true, at: 1 } })
function readCopies(): Copies {
  try { const c = JSON.parse(localStorage.getItem(COPIES) || 'null'); if (c && typeof c === 'object') return c } catch {}
  return firstCopy()
}
function writeCopies(c: Copies) { try { localStorage.setItem(COPIES, JSON.stringify(c)) } catch {} }
/** The servers keeping copies of this browser's list, this app's own first. */
export function copyServers(): string[] {
  const c = readCopies()
  return Object.keys(c).filter((s) => c[s]?.on && normalServer(s) === s)
    .sort((a, b) => (a === location.origin ? -1 : b === location.origin ? 1 : a.localeCompare(b)))
}
/** Keep a copy of the list on `server`, or stop (what's there is left, marked as no longer a copy). */
export async function setCopy(server: string, on: boolean) {
  writeCopies({ ...readCopies(), [server]: { on, at: Date.now() } })
  if (!on) await leaveNote(server).catch(() => {})
  await syncNow()
}
/** Why a copy wasn't reached or written at the last sync (a ListError code, or 'network'). */
export const copyProblems = new Map<string, string>()

// ---- a server -----------------------------------------------------------------------------
async function pull<T>(key: ListKey, server = location.origin): Promise<{ seq: number; doc: T } | null> {
  const got = await getRaw(server, key.id)
  if (got === 'retired') throw new ListError('list-retired')
  return got && { seq: got.seq, doc: JSON.parse(dec.decode(await key.decrypt(got.data))) as T }
}

/** False if someone else got there first. */
async function push(key: ListKey, seq: number, doc: unknown, server = location.origin): Promise<boolean> {
  return putRaw(server, key.id, seq, await key.encrypt(enc.encode(canon(doc))), key.signingSeed)
}

/** An entry as stored (encrypted), by id: null if missing, 'retired' if retired. */
async function getRaw(server: string, id: string): Promise<{ seq: number; data: Uint8Array } | null | 'retired'> {
  const r = await fetch(`${server}/api/list/${id}`, { cache: 'no-store', headers: APP_HEADER })
  if (r.status === 404) return null
  if (r.status === 426) throw new ListError('update-needed')
  if (r.status === 410) return 'retired'
  if (!r.ok) throw new ListError('server')
  return { seq: Number(r.headers.get('X-Seq')), data: new Uint8Array(await r.arrayBuffer()) }
}

/** Write an entry as stored, signed with the seed of its signing key (`signPut`). */
async function putRaw(server: string, id: string, seq: number, data: Uint8Array, seed: string): Promise<boolean> {
  const headers: Record<string, string> = { ...APP_HEADER, 'X-Seq': String(seq), 'X-Sig': signPut(seed, id, seq, data), 'Content-Type': 'application/octet-stream' }
  const code = creationCode.get(server)
  if (code) headers['X-Create-Code'] = code
  const r = await fetch(`${server}/api/list/${id}`, { method: 'PUT', headers, body: data as BodyInit })
  if (r.status === 409) return false
  if (!r.ok) throw new ListError(await r.json().then((x) => String(x.code ?? 'server'), () => 'server'))
  return true
}

/** Read an entry on one server, change it, and write it back unless that changed nothing, retrying
 *  if another device got there first. A missing entry that would equal `blank` isn't created. */
async function updateOn<T>(server: string, key: ListKey, change: (had: T | null) => T, blank?: T): Promise<T> {
  for (let i = 0; i < 5; i++) {
    const had = await pull<T>(key, server)
    const before = had ? canon(had.doc) : blank === undefined ? null : canon(blank) // `change` may alter what it is given
    const next = change(had?.doc ?? null)
    if (before === canon(next)) return next
    if (await push(key, (had?.seq ?? 0) + 1, next, server)) return next
  }
  throw new ListError('busy')
}

/** One entry on every copy: each read and merged into what's known so far (`merge(had, known)`,
 *  starting from `mine`), and written back where that adds something; then the copies written
 *  before others added more are written again. A copy that can't be reached is skipped (it catches
 *  up later), but one is needed; a copy that says the list was retired stops it. `blank` per server:
 *  see updateOn. */
async function replicate<T>(key: ListKey, merge: (had: T | null, known: T) => T, mine: T, blank?: (server: string) => T | undefined): Promise<T> {
  let known = mine, failure: unknown = null
  const holds = new Map<string, string>() // server -> what it holds now (canon)
  for (const s of copyServers()) {
    try {
      known = await updateOn(s, key, (had) => merge(had, known), blank?.(s))
      holds.set(s, canon(known)); copyProblems.delete(s)
    } catch (e) {
      if (e instanceof ListError && e.message === 'list-retired') throw e
      failure = e; copyProblems.set(s, e instanceof ListError ? e.message : 'network')
    }
  }
  if (!holds.size) throw failure ?? new ListError('server')
  for (const [s, held] of holds) {
    if (held === canon(known)) continue
    try { known = await updateOn(s, key, (had) => merge(had, known), blank?.(s)) } catch { /* next sync */ }
  }
  return known
}

/** Merge this browser's list with every copy, and update those that lacked something. */
const exchange = (key: ListKey, local: ListDoc) => replicate<ListDoc>(key, (had, known) => forgetOldRemovals(mergeLists(had ?? emptyList(), known)), local)

/** Delete an entry on one server, keeping its id out of use, given the seed of its signing key. */
async function retire(id: string, seed: string, server = location.origin) {
  const sig = b64e(ed25519.sign(enc.encode(`listdel:${id}`), b64d(seed)))
  const r = await fetch(`${server}/api/list/${id}`, { method: 'DELETE', headers: { ...APP_HEADER, 'X-Sig': sig } })
  if (!r.ok && r.status !== 404 && r.status !== 410) throw new ListError('server')
}

/** On a server that no longer keeps a copy: its settings say so (copy:<it> off), so a device that
 *  reaches only it learns where the copies are. What's there is left as it was. */
async function leaveNote(server: string) {
  const secret = savedSecret()
  if (!secret) return
  await updateOn<SettingsDoc>(server, await settingsKey(secret), (had) => mergeSettings(had ?? emptySettings(), localSettings()), emptySettings())
}

// ---- tags ----------------------------------------------------------------------------------
const tagsKey = async (secret: Uint8Array) => ListKey.of(await hmac(secret, 'texloom list tags'))

/** Merge this browser's tags with the server's. A new entry needs a creation code, which a device
 *  that only unlocked the list may not have: so a device with one makes the entry straight away. */
async function exchangeTags(secret: Uint8Array, list: ListDoc): Promise<boolean> {
  const listed = new Set(Object.keys(list.projects))
  const doc = await replicate<TagDoc>(await tagsKey(secret), (had, known) => tidyTags(mergeTags(had ?? emptyTags(), known), listed), tags.doc(),
    (server) => (creationCode.get(server) ? undefined : emptyTags()))
  return tags.apply(tidyTags(mergeTags(tags.doc(), doc), listed)) // tags may have changed meanwhile
}

// ---- settings: the creation code follows the list -----------------------------------------
const settingsKey = async (secret: Uint8Array) => ListKey.of(await hmac(secret, 'texloom list settings'))

/** Settings that follow the list: your name, and Zotero and git hosts' accounts, each with when
 *  it was set (one from before that was recorded counts as older than any that was). */
/** An account (accounts.ts) as a setting: JSON, '' for none. */
const accountSetting = (s: AccountStore<object>) => ({
  get: () => { const a = s.get(); return a ? JSON.stringify(a) : '' },
  at: () => s.at(),
  set: (v: string, at: number) => { try { s.set(v ? JSON.parse(v) : null, at) } catch {} },
})

const SETTINGS: Record<string, { get(): string; at(): number; set(v: string, at: number): void }> = {
  name: { get: () => myName.get(), at: () => myName.at(), set: (v, at) => myName.set(v, at, true) },
  zotero: accountSetting(zoteroAccount),
  github: accountSetting(githubAccount),
  git: accountSetting(otherAccounts), // GitLab, Gitea and Forgejo accounts
  dropbox: accountSetting(dropboxAccount),
}

/** SETTINGS, with a code:<server> entry for each server whose creation code this browser holds or
 *  the list carries (named by server, as the list may be kept on several). The desktop app reads
 *  the same keys. */
const CODE_FOR = 'code:'
function settingsFor(names: Iterable<string> = []): typeof SETTINGS {
  const all = { ...SETTINGS }
  const mine = [location.origin, ...creationCode.servers()].filter((s) => creationCode.get(s) || creationCode.at(s))
  for (const name of [...names, ...mine.map((s) => CODE_FOR + s)]) {
    const server = name.startsWith(CODE_FOR) ? normalServer(name.slice(CODE_FOR.length)) : null
    if (!server || server !== name.slice(CODE_FOR.length) || all[name]) continue
    all[name] = { get: () => creationCode.get(server), at: () => creationCode.at(server), set: (v, at) => creationCode.set(v, at, server) }
  }
  return all
}

const COPY = 'copy:' // copy:<origin>: whether that server keeps a copy of the list ("1" or "")

function localSettings(): SettingsDoc {
  const doc = emptySettings()
  for (const [name, s] of Object.entries(settingsFor())) {
    const v = s.get(), at = s.at()
    if (v || at) doc.s[name] = { v, at: at || 1 }
  }
  for (const [server, c] of Object.entries(readCopies())) if (normalServer(server) === server) doc.s[COPY + server] = { v: c.on ? '1' : '', at: c.at }
  return doc
}

/** The copies the list's settings name, taken on here. True if they changed. */
function takeCopies(doc: SettingsDoc): boolean {
  const had = readCopies(), next: Copies = { ...had }
  for (const [name, v] of Object.entries(doc.s)) {
    const server = name.startsWith(COPY) ? normalServer(name.slice(COPY.length)) : null
    if (!server || server !== name.slice(COPY.length)) continue
    const mine = had[server]
    if (!mine || v.at > mine.at || (v.at === mine.at && (v.v === '1') !== mine.on)) next[server] = { on: v.v === '1', at: v.at }
  }
  if (canon(next) === canon(had)) return false
  writeCopies(next)
  return true
}

/** Merge these settings with the list's, so what's entered on one device works on the others
 *  (and, for the creation code, in the TeXLoom app). True if any changed here. */
async function exchangeSettings(secret: Uint8Array): Promise<boolean> {
  const local = localSettings()
  const key = await settingsKey(secret)
  let doc = await replicate<SettingsDoc>(key, (had, known) => mergeSettings(had ?? emptySettings(), known), local, () => emptySettings())
  if (takeCopies(doc)) doc = await replicate<SettingsDoc>(key, (had, known) => mergeSettings(had ?? emptySettings(), known), doc, () => emptySettings()) // new copies: they get the settings too
  let changed = false
  for (const [name, s] of Object.entries(settingsFor(Object.keys(doc.s)))) {
    const got = doc.s[name]
    if (!got || canon(got) === canon(local.s[name]) || name.startsWith(COPY)) continue
    s.set(got.v, got.at)
    changed = true
  }
  return changed
}

// ---- this browser --------------------------------------------------------------------------
let lastSynced = ''
let tagsError: string | null = null
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

/** Use this list on this browser, or none. `server`: one keeping a copy of it (where its link
 *  points); the others are learnt from the list's settings. */
export function saveSecret(secret: Uint8Array | null, server = location.origin) {
  try {
    if (secret) localStorage.setItem(STORE, b64e(secret))
    else localStorage.removeItem(STORE)
  } catch {}
  writeCopies(firstCopy(server))
  copyProblems.clear()
  lastSynced = ''
  lastError = null
  tagsError = null
}

export const lastProblem = () => lastError
/** Why tags aren't reaching the server, if they aren't; the list itself may be fine. */
export const tagsProblem = () => tagsError
const localState = () => canon({ list: projects.asList(), tags: tags.doc(), settings: localSettings() })

export function syncNow(): Promise<void> {
  if (!savedSecret()) return Promise.resolve()
  return (running ??= (async () => {
    try {
      lastAttempt = Date.now()
      const secret = savedSecret()!
      let different = false
      try { different = await exchangeSettings(secret) } catch (e) { // first: they say where the copies are
        if (e instanceof ListError && e.message === 'list-retired') throw e
        console.warn('list settings:', e)
      }
      const doc = await exchange(await ListKey.of(secret), projects.asList())
      different = projects.applyList(doc) || different
      const hadError = tagsError
      try {
        different = await exchangeTags(secret, doc) || different
        tagsError = null
      } catch (e) { tagsError = e instanceof ListError ? e.message : 'network' }
      await copyWays(secret).catch((e) => console.warn('list ways in:', e))
      lastSynced = localState()
      lastError = null
      if (different || tagsError !== hadError) changed()
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
  timer = setTimeout(() => { if (localState() !== lastSynced) syncQuietly() }, 3000)
}

/** Keep this browser's list up to date; `onChange` runs when another device changed it. */
export function startListSync(onChange: () => void) {
  changed = onChange
  projects.onChange(schedule)
  tags.onChange(schedule)
  window.addEventListener(NAME_CHANGED, schedule)
  syncQuietly()
  window.addEventListener('focus', () => { if (Date.now() - lastAttempt > 60_000) syncQuietly() })
  window.addEventListener('online', syncQuietly)
}

export async function adopt(secret: Uint8Array, server = location.origin) {
  saveSecret(secret, server)
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
/** The ways in, and those removed (id -> when), so that a copy that missed a removal can't bring one back. */
type Registry = { v: 1; slots: Record<string, Slot>; gone?: Record<string, number> }
const noSlots = (): Registry => ({ v: 1, slots: {} })
function mergeRegistry(a: Registry, b: Registry): Registry {
  const gone: Record<string, number> = { ...(a.gone ?? {}) }
  for (const [id, at] of Object.entries(b.gone ?? {})) gone[id] = Math.max(gone[id] ?? 0, at)
  const slots: Record<string, Slot> = {}
  for (const [id, slot] of [...Object.entries(a.slots ?? {}), ...Object.entries(b.slots ?? {})]) {
    if ((gone[id] ?? 0) >= slot.at) continue
    if (!slots[id] || slot.at > slots[id].at) slots[id] = slot
  }
  return { v: 1, slots, ...(Object.keys(gone).length ? { gone } : {}) }
}

const registryKey = async (secret: Uint8Array) => ListKey.of(await hmac(secret, 'texloom list slots'))

/** The ways in, as all the copies know them (and the removed ones). */
async function registry(secret: Uint8Array): Promise<Registry> {
  return replicate<Registry>(await registryKey(secret), (had, known) => mergeRegistry(had ?? noSlots(), known), noSlots(), () => noSlots())
}

export async function readSlots(secret: Uint8Array): Promise<Record<string, Slot>> {
  return (await registry(secret)).slots
}

async function changeSlots(secret: Uint8Array, change: Registry) {
  await replicate<Registry>(await registryKey(secret), (had, known) => mergeRegistry(had ?? noSlots(), known), change)
}

/** Make `unlocking` a way into the list; returns its id. */
export async function addSlot(secret: Uint8Array, unlocking: Uint8Array, kind: Slot['kind']): Promise<string> {
  const key = await ListKey.of(unlocking)
  // a passkey only works on the server it was made on (its address); a password works on every copy
  // (a copy that can't be reached now gets it at a later sync: copyWays)
  let made = 0, failure: unknown = null
  for (const server of kind === 'password' ? copyServers() : [location.origin]) {
    try {
      await updateOn<{ v: 1; wrap: string }>(server, key, (had) => {
        if (had && kind === 'password') throw new ListError('password-taken')
        return { v: 1, wrap: b64e(secret) }
      })
      made++
    } catch (e) {
      if (e instanceof ListError && (e.message === 'password-taken' || e.message === 'list-retired')) throw new ListError('password-taken') // (retired: used before, then changed)
      failure = e
    }
  }
  if (!made) throw failure ?? new ListError('server')
  await changeSlots(secret, { v: 1, slots: { [key.id]: { kind, at: Date.now(), seed: key.signingSeed } } })
  return key.id
}

export async function removeSlot(secret: Uint8Array, id: string, slot: Slot) {
  for (const server of copyServers()) await retire(id, slot.seed, server).catch(() => {}) // (one offline: copyWays, later)
  await changeSlots(secret, { v: 1, slots: {}, gone: { [id]: Date.now() } })
}

/** Passwords on every copy (each entry copied as it is, encrypted; it's signed with the seed the
 *  record of ways in keeps), and removed ways in gone from every copy. */
async function copyWays(secret: Uint8Array) {
  const reg = await registry(secret)
  const servers = copyServers()
  for (const [id, slot] of Object.entries(reg.slots)) {
    if (slot.kind !== 'password') continue
    let blob: { seq: number; data: Uint8Array } | null = null
    const lacking: string[] = []
    for (const s of servers) {
      const got = await getRaw(s, id).catch(() => undefined)
      if (got === null) lacking.push(s)
      else if (got && got !== 'retired') blob ??= got
    }
    if (blob) for (const s of lacking) await putRaw(s, id, 1, blob.data, slot.seed).catch((e) => copyProblems.set(s, e instanceof ListError ? e.message : 'network'))
  }
  // (A removed one is retired on the copies reachable then; one that wasn't keeps a stale entry,
  // but its seed went with the record, and the password it stood for has been replaced.)
}

/** The list's secret, from a way in; null if that doesn't open anything. Every server this browser
 *  knows is tried: this one first, then those keeping its list. */
async function openSlot(unlocking: Uint8Array): Promise<Uint8Array | null> {
  const key = await ListKey.of(unlocking)
  for (const server of [...new Set([location.origin, ...copyServers()])]) {
    const had = await pull<{ wrap: string }>(key, server).catch((e) => {
      if (e instanceof ListError && e.message === 'list-retired') return null // removed
      if (server === location.origin) throw e
      return null
    })
    if (had) return b64d(had.doc.wrap)
  }
  return null
}

/** The list's secret from a passkey: null if cancelled, 'none' if it opens nothing here. */
export async function unlockWithPasskey(): Promise<Uint8Array | 'none' | null> {
  const unlocking = await usePasskey()
  return unlocking ? (await openSlot(unlocking)) ?? 'none' : null
}

export const MIN_PASSWORD = 12

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

/** Delete a list and every way into it on every copy, keeping their ids out of use. A copy that
 *  can't be reached now keeps them, but a device still using the list finds it retired on the
 *  others, and a list retired on any copy is retired. */
async function retireList(secret: Uint8Array) {
  const slots = await readSlots(secret)
  const keys = [await registryKey(secret), await tagsKey(secret), await settingsKey(secret), await ListKey.of(secret)]
  let done = 0
  for (const server of copyServers()) {
    try {
      for (const [id, slot] of Object.entries(slots)) await retire(id, slot.seed, server)
      for (const key of keys) await retire(key.id, key.signingSeed, server)
      done++
    } catch { /* unreachable now */ }
  }
  if (!done) throw new ListError('server')
}

/** Move the list to a new secret, for when the link has got out. Everything that opened the old
 *  one stops working, including passkeys and passwords: whoever has the old link could read them. */
export async function replaceList(secret: Uint8Array) {
  await syncNow()
  const fresh = newSecret(), copies = readCopies()
  await exchange(await ListKey.of(fresh), projects.asList()) // on every copy
  await retireList(secret)
  saveSecret(fresh)
  writeCopies(copies) // the same servers keep it
  await syncNow()
}

export async function deleteList(secret: Uint8Array) {
  await retireList(secret)
  saveSecret(null)
}
