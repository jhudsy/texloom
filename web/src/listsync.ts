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
import { canon, emptyList, emptySettings, emptyTags, forgetOldRemovals, hmac, ListKey, mergeLists, mergeSettings, mergeTags, passwordSecret, tidyTags, type ListDoc, type SettingsDoc, type TagDoc } from './listcore'
import { projects } from './projects'
import { creationCode } from './replica'
import { zoteroAccount } from './zotero'
import { tags } from './tags'

/** The message is a code: needs-code, code-expired, list-full, list-retired, password-taken,
 *  wrong-password, no-prf, passkey-failed, busy or server. */
export class ListError extends Error {}

const STORE = 'texloom.listKey'
const PASSKEY_SALT = new TextEncoder().encode('texloom project list v1')
const enc = new TextEncoder(), dec = new TextDecoder()

export const newSecret = () => crypto.getRandomValues(new Uint8Array(32))
export const linkFor = (secret: Uint8Array) => `${location.origin}/#sync=${b64e(secret)}`

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
 *  device got there first. A missing entry that would equal `blank` isn't created. */
async function update<T>(key: ListKey, change: (had: T | null) => T, blank?: T): Promise<T> {
  for (let i = 0; i < 5; i++) {
    const had = await pull<T>(key)
    const before = had ? canon(had.doc) : blank === undefined ? null : canon(blank) // `change` may alter what it is given
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

// ---- tags ----------------------------------------------------------------------------------
const tagsKey = async (secret: Uint8Array) => ListKey.of(await hmac(secret, 'texloom list tags'))

/** Merge this browser's tags with the server's. A new entry needs a creation code, which a device
 *  that only unlocked the list may not have: so a device with one makes the entry straight away. */
async function exchangeTags(secret: Uint8Array, list: ListDoc): Promise<boolean> {
  const listed = new Set(Object.keys(list.projects))
  const doc = await update<TagDoc>(await tagsKey(secret), (had) => tidyTags(mergeTags(had ?? emptyTags(), tags.doc()), listed), creationCode.get() ? undefined : emptyTags())
  return tags.apply(tidyTags(mergeTags(tags.doc(), doc), listed)) // tags may have changed meanwhile
}

// ---- settings: the creation code follows the list -----------------------------------------
const settingsKey = async (secret: Uint8Array) => ListKey.of(await hmac(secret, 'texloom list settings'))

/** Settings that follow the list: this browser's creation code and Zotero account, each with when
 *  it was set (one from before that was recorded counts as older than any that was). */
const SETTINGS: Record<string, { get(): string; at(): number; set(v: string, at: number): void }> = {
  code: { get: () => creationCode.get(), at: () => creationCode.at(), set: (v, at) => creationCode.set(v, at) },
  zotero: {
    get: () => { const a = zoteroAccount.get(); return a ? JSON.stringify(a) : '' },
    at: () => zoteroAccount.at(),
    set: (v, at) => { try { zoteroAccount.set(v ? JSON.parse(v) : null, at) } catch {} },
  },
}

function localSettings(): SettingsDoc {
  const doc = emptySettings()
  for (const [name, s] of Object.entries(SETTINGS)) {
    const v = s.get(), at = s.at()
    if (v || at) doc.s[name] = { v, at: at || 1 }
  }
  return doc
}

/** Merge these settings with the list's, so what's entered on one device works on the others
 *  (and, for the creation code, in the TeXLoom app). True if any changed here. */
async function exchangeSettings(secret: Uint8Array): Promise<boolean> {
  const local = localSettings()
  const doc = await update<SettingsDoc>(await settingsKey(secret), (had) => mergeSettings(had ?? emptySettings(), local), emptySettings())
  let changed = false
  for (const [name, s] of Object.entries(SETTINGS)) {
    const got = doc.s[name]
    if (!got || canon(got) === canon(local.s[name])) continue
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

/** Use this list on this browser, or none. */
export function saveSecret(secret: Uint8Array | null) {
  try {
    if (secret) localStorage.setItem(STORE, b64e(secret))
    else localStorage.removeItem(STORE)
  } catch {}
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
      const doc = await exchange(await ListKey.of(secret), projects.asList())
      let different = projects.applyList(doc)
      const hadError = tagsError
      try {
        different = await exchangeTags(secret, doc) || different
        tagsError = null
      } catch (e) { tagsError = e instanceof ListError ? e.message : 'network' }
      try { different = await exchangeSettings(secret) || different } catch (e) { console.warn('list settings:', e) }
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
  for (const key of [await registryKey(secret), await tagsKey(secret), await settingsKey(secret), await ListKey.of(secret)]) await retire(key.id, key.signingSeed)
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
