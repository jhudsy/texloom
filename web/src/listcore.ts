// The parts of the project list that must match texloom/listsync.py exactly: keys, merging and the
// password function. Nothing here needs a browser, so scripts/check-listsync.mjs can test it
// against scripts/listsync-vectors.json, as scripts/check_listsync.py does for the Python.
import { ed25519 } from '@noble/curves/ed25519.js'
import { scryptAsync } from '@noble/hashes/scrypt.js'
import { sha256 } from '@noble/hashes/sha2.js'
import { b64d, b64e, concat, seal, unseal } from './crypto'

/** s: the project's server (a project lives on one server; the list may be kept on several). An entry
 *  without it is on the server that keeps the list. */
export interface ListEntry { f: string; n: string; at: number; w: boolean; s?: string; nick?: string; t?: string; by?: string; c?: number; m?: number }
export interface ListDoc { v: 1; projects: Record<string, ListEntry>; gone: Record<string, number> }

const YEAR = 365 * 86400_000
const enc = new TextEncoder()

// ---- keys ----------------------------------------------------------------------------------
export const hmac = async (key: Uint8Array, label: string) => new Uint8Array(await crypto.subtle.sign('HMAC',
  await crypto.subtle.importKey('raw', key as BufferSource, { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']), enc.encode(label) as BufferSource))

export class ListKey {
  private constructor(readonly secret: Uint8Array, readonly id: string, private seed: Uint8Array, private aes: CryptoKey) {}

  static async of(secret: Uint8Array): Promise<ListKey> {
    const seed = await hmac(secret, 'texloom list sign')
    const aes = await crypto.subtle.importKey('raw', await hmac(secret, 'texloom list encrypt') as BufferSource, 'AES-GCM', false, ['encrypt', 'decrypt'])
    return new ListKey(secret, b64e(ed25519.getPublicKey(seed)), seed, aes)
  }

  encrypt(plain: Uint8Array): Promise<Uint8Array> { return seal(this.aes, plain) }
  decrypt(data: Uint8Array): Promise<Uint8Array> { return unseal(this.aes, data) }

  get signingSeed(): string { return b64e(this.seed) }
}

/** The signature on writing entry `id` as `seq` (stored data `data`), with its signing seed (ListKey.signingSeed). */
export const signPut = (seed: string, id: string, seq: number, data: Uint8Array): string =>
  b64e(ed25519.sign(concat(enc.encode(`list:${id}:${seq}:`), data), b64d(seed)))

// ---- merging -------------------------------------------------------------------------------
export const emptyList = (): ListDoc => ({ v: 1, projects: {}, gone: {} })

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
 *  wins, except that an edit link is never replaced by a read-only one, and the dates keep the
 *  earliest creation and the latest change; removed projects stay removed unless they were opened
 *  again afterwards. */
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
      if (x.c && y.c) e.c = Math.min(x.c, y.c)
      if (x.m && y.m) e.m = Math.max(x.m, y.m)
    } else e = { ...(x ?? y)! }
    if (e.at > (out.gone[p] ?? 0)) out.projects[p] = e
  }
  return out
}

export function forgetOldRemovals(doc: ListDoc): ListDoc {
  const cutoff = Date.now() - YEAR
  return { ...doc, gone: Object.fromEntries(Object.entries(doc.gone).filter(([, when]) => when >= cutoff)) }
}

// ---- tags ----------------------------------------------------------------------------------
/** A person's tags: never in a project, and in an entry of their own so that clients which don't
 *  know about them can't drop them. `on` holds, per project and tag, when it was last assigned,
 *  negative if that was to take it off. A tag's `c` is the colour chosen for it, if any. */
export interface TagDoc { v: 1; tags: Record<string, { n: string; at: number; gone?: boolean; c?: string }>; on: Record<string, Record<string, number>> }

export const emptyTags = (): TagDoc => ({ v: 1, tags: {}, on: {} })

/** The order and length matter: a tag nobody has chosen a colour for gets one from its id, the
 *  same on every device, and changing this list would repaint those tags. */
export const TAG_COLOURS = ['red', 'orange', 'yellow', 'green', 'teal', 'blue', 'indigo', 'purple', 'pink', 'grey'] as const
export type TagColour = typeof TAG_COLOURS[number]

export const tagColour = (id: string, chosen?: string): TagColour => TAG_COLOURS.find((c) => c === chosen)
  ?? TAG_COLOURS[[...id].reduce((sum, c) => (sum * 31 + c.charCodeAt(0)) >>> 0, 0) % TAG_COLOURS.length]

/** The form in which two tag names count as the same. */
export const foldTag = (name: string) => name.normalize('NFKC').replace(/\s+/g, ' ').trim().toLowerCase()

const laterMark = (a: number, b: number) => Math.abs(a) !== Math.abs(b) ? (Math.abs(a) > Math.abs(b) ? a : b) : Math.max(a, b)

/** Two sets of tags as one, the same whichever way round. Each tag, and each tag on each project,
 *  takes its latest change; on a tie, deleting and assigning win. Tags given the same name apart
 *  (two devices, or a rename) become one, which has every project either had. */
export function mergeTags(a: TagDoc, b: TagDoc): TagDoc {
  const out = emptyTags()
  for (const id of new Set([...Object.keys(a.tags ?? {}), ...Object.keys(b.tags ?? {})])) {
    const x = a.tags?.[id], y = b.tags?.[id]
    if (!x || !y) { out.tags[id] = { ...(x ?? y)! }; continue }
    const xWins = x.at !== y.at ? x.at > y.at : !!x.gone !== !!y.gone ? !!x.gone : canon(x) >= canon(y)
    out.tags[id] = { ...(xWins ? x : y) }
  }
  for (const p of new Set([...Object.keys(a.on ?? {}), ...Object.keys(b.on ?? {})])) {
    const marks: Record<string, number> = out.on[p] = { ...a.on?.[p] }
    for (const [id, when] of Object.entries(b.on?.[p] ?? {})) marks[id] = id in marks ? laterMark(marks[id], when) : when
  }
  const named = new Map<string, string>()
  for (const id of Object.keys(out.tags).sort()) {
    const tag = out.tags[id]
    if (tag.gone) continue
    const keep = named.get(foldTag(tag.n))
    if (!keep) { named.set(foldTag(tag.n), id); continue }
    out.tags[id] = { n: '', at: tag.at, gone: true }
    for (const marks of Object.values(out.on)) {
      if (!(id in marks)) continue
      marks[keep] = keep in marks ? laterMark(marks[keep], marks[id]) : marks[id]
      delete marks[id]
    }
  }
  return out
}

/** Drop what is no longer needed: old deletions, marks for deleted tags, and projects that left the list. */
export function tidyTags(doc: TagDoc, listed: Set<string>, now = Date.now()): TagDoc {
  const cutoff = now - YEAR
  const out = emptyTags()
  for (const [id, tag] of Object.entries(doc.tags)) if (!tag.gone || tag.at >= cutoff) out.tags[id] = tag
  for (const [p, marks] of Object.entries(doc.on)) {
    if (!listed.has(p)) continue
    const kept = Object.entries(marks).filter(([id, when]) => out.tags[id] && !out.tags[id].gone && (when > 0 || -when >= cutoff))
    if (kept.length) out.on[p] = Object.fromEntries(kept)
  }
  return out
}

// ---- settings that follow the list (an entry of their own, like tags) ------------------------
/** Settings that travel with the list: the creation codes (code:<server>), the servers keeping
 *  copies (copy:<server>), your name, and the Zotero, GitHub, GitLab/Gitea/Forgejo and Dropbox
 *  accounts with their tokens; each with when it was set. The latest wins, and a tie goes the same
 *  way on every device. A time more than SETTING_SKEW ahead counts as now + SETTING_SKEW, so that
 *  a setting dated far in the future (by someone who had the list's link, say) can't stay ahead of
 *  every later change. */
export interface SettingsDoc { v: 1; s: Record<string, { v: string; at: number }> }
export const emptySettings = (): SettingsDoc => ({ v: 1, s: {} })
export const SETTING_SKEW = 10 * 60_000

export function mergeSettings(a: SettingsDoc, b: SettingsDoc, now = Date.now()): SettingsDoc {
  const out = emptySettings(), limit = now + SETTING_SKEW
  const capped = (x?: { v: string; at: number }) => (x && x.at > limit ? { ...x, at: limit } : x)
  for (const name of [...new Set([...Object.keys(a.s ?? {}), ...Object.keys(b.s ?? {})])].sort()) {
    const x = capped(a.s?.[name]), y = capped(b.s?.[name])
    out.s[name] = !x ? y! : !y ? x : x.at > y.at || (x.at === y.at && canon(x) >= canon(y)) ? x : y
  }
  return out
}

// ---- usernames and passwords ------------------------------------------------------------------
// What counts as space around a username: the same set texloom/listsync.py strips (JavaScript's
// trim() and Python's strip() each miss some of the other's).
const EDGES = '\\t\\n\\v\\f\\r \\x1c-\\x1f\\x85\\xa0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000\\ufeff'
const AROUND = new RegExp(`^[${EDGES}]+|[${EDGES}]+$`, 'g')
/** A username as it is salted: without space around it, in lower case, NFKC. */
export const normUsername = (username: string) => username.replace(AROUND, '').toLowerCase().normalize('NFKC')

/** What a username and password stand for. Slow on purpose, and done on this device: neither is
 *  ever sent anywhere. */
export const passwordSecret = (username: string, password: string) => scryptAsync(
  enc.encode(password.normalize('NFKC')),
  enc.encode(`texloom list password v1:${normUsername(username)}`),
  { N: 2 ** 17, r: 8, p: 1, dkLen: 32, maxmem: 256 * 1024 * 1024 + 1024 },
)

/** Passwords people pick most, and keyboard runs: in a new password each counts as one guess of a
 *  few hundred, not as its letters. Must match texloom/listsync.py (scripts/listsync-vectors.json). */
export const COMMON_PASSWORDS = [
  'password', 'passw0rd', 'qwerty', 'qwertyuiop', 'asdfgh', 'asdfghjkl', 'zxcvbn', 'zxcvbnm', 'qazwsx', '1qaz2wsx', 'azerty', 'qwertz',
  'letmein', 'welcome', 'iloveyou', 'iloveu', 'admin', 'administrator', 'login', 'master', 'monkey', 'dragon', 'football',
  'baseball', 'soccer', 'hockey', 'princess', 'sunshine', 'shadow', 'superman', 'batman', 'trustno1', 'starwars', 'pokemon',
  'charlie', 'michael', 'jennifer', 'jordan', 'hunter', 'buster', 'freedom', 'whatever', 'secret', 'computer', 'internet',
  'samsung', 'google', 'apple', 'cheese', 'chocolate', 'summer', 'winter', 'spring', 'autumn', 'flower', 'butterfly',
  'purple', 'orange', 'banana', 'cookie', 'pepper', 'ginger', 'liverpool', 'chelsea', 'arsenal', 'killer', 'mustang',
  'harley', 'ranger', 'tigger', 'love', 'lovely', 'loveme', 'angel', 'baby', 'babygirl', 'family', 'friends', 'hello',
  'abc123', 'test', 'testing', 'guest', 'default', 'changeme', 'temp', 'access', 'passport', 'matrix', 'nothing', 'thomas',
  'daniel', 'andrew', 'robert', 'jessica', 'ashley', 'nicole', 'maggie', 'texloom', 'latex', 'overleaf', 'thesis', 'project',
  'sync', 'mylist', 'mypassword', 'passwort', 'motdepasse', 'contrasena', 'senha', 'correcthorsebatterystaple', 'qwer',
  'asdf', 'zxcv',
]
export const MIN_PASSWORD = 12
export const MIN_PASSWORD_BITS = 60
const LEET: Record<string, string> = { '0': 'o', '1': 'l', '3': 'e', '4': 'a', '5': 's', '7': 't', '@': 'a', '$': 's', '!': 'i' }
const fold = (c: string) => LEET[c] ?? (c >= 'A' && c <= 'Z' ? c.toLowerCase() : c)

/** How hard a new password is to guess, roughly (bits, rounded down), and what's wrong with it:
 *  'short' (under MIN_PASSWORD characters), 'weak' (under 6 different characters, or under
 *  MIN_PASSWORD_BITS), or null. Repeats (aaaa, abab), runs (abcd, 4321), common passwords and the
 *  username count for little. Only for setting one: a password set before still opens the list.
 *  Must match texloom/listsync.py password_strength. */
export function passwordStrength(password: string, username = ''): { bits: number; problem: 'short' | 'weak' | null } {
  const cs = [...password.normalize('NFKC')], n = cs.length
  const cp = cs.map((c) => c.codePointAt(0)!)
  const t = cs.map(fold)
  const words = [...new Set([...COMMON_PASSWORDS, ...normUsername(username).split(/[^a-z0-9]+/)].map((w) => [...w].map(fold).join('')))]
    .filter((w) => w.length >= 3).sort((a, b) => b.length - a.length || (a < b ? -1 : a > b ? 1 : 0))
  const covered: boolean[] = new Array(n).fill(false)
  let found = 0
  for (const w of words) {
    for (let i = 0; i + w.length <= n; i++) {
      if (t.slice(i, i + w.length).join('') !== w || covered.slice(i, i + w.length).some(Boolean)) continue
      covered.fill(true, i, i + w.length)
      found++
    }
  }
  let fresh = 0
  for (let i = 0; i < n; i++) {
    if (covered[i]) continue
    if (i >= 1 && cp[i] === cp[i - 1]) continue // aaaa
    if (i >= 2 && Math.abs(cp[i] - cp[i - 1]) === 1 && cp[i] - cp[i - 1] === cp[i - 1] - cp[i - 2]) continue // abcd, 4321
    let again = false // abab, xyzxyz
    for (let p = 2; p <= i - 1 && !again; p++) again = cp[i] === cp[i - p] && cp[i - 1] === cp[i - 1 - p]
    if (!again) fresh++
  }
  const kind = (c: number) => (c >= 97 && c <= 122 ? 'a' : c >= 65 && c <= 90 ? 'A' : c >= 48 && c <= 57 ? '0' : c >= 32 && c <= 126 ? '!' : '*')
  const POOL: Record<string, number> = { a: 26, A: 26, '0': 10, '!': 33, '*': 100 }
  const pool = [...new Set(cp.map(kind))].reduce((sum, k) => sum + POOL[k], 0)
  const bits = Math.floor(fresh * Math.log2(Math.max(pool, 1)) + 8 * found)
  const problem = n < MIN_PASSWORD ? 'short' : new Set(cs).size < 6 || bits < MIN_PASSWORD_BITS ? 'weak' : null
  return { bits, problem }
}

/** A passphrase to suggest: four made-up words of three syllables each (80 syllables: about 75 bits). */
export function suggestPassphrase(): string {
  const CONS = 'bdfghjklmnprstvz', VOWELS = 'aeiou'
  for (;;) {
    const syllables: string[] = []
    for (const r of crypto.getRandomValues(new Uint8Array(32))) {
      if (r < 240 && syllables.length < 12) syllables.push(CONS[Math.floor((r % 80) / 5)] + VOWELS[r % 5])
    }
    if (syllables.length < 12) continue
    const out = [0, 3, 6, 9].map((i) => syllables.slice(i, i + 3).join('')).join('-')
    if (!passwordStrength(out).problem) return out
  }
}

// ---- what's stored ----------------------------------------------------------------------------
/** Entries are stored padded with spaces (which JSON ignores) to a multiple of PAD_TO bytes, so that
 *  a password's or passkey's small entry is the same size as a small list, its tags or settings. */
export const PAD_TO = 512
export function padded(json: string): Uint8Array {
  const b = enc.encode(json)
  const out = new Uint8Array(Math.max(PAD_TO, Math.ceil(b.length / PAD_TO) * PAD_TO)).fill(0x20)
  out.set(b)
  return out
}

/** A way in's entry as stored (encrypted), as the record of ways in keeps it, so that a copy holding
 *  something else in its place can't have that copied to the other servers. */
export const slotHash = (data: Uint8Array) => b64e(sha256(data))

// ---- the ways in (passkeys and passwords) -----------------------------------------------
/** h: slotHash of its entry (a password without one isn't copied to other servers). */
export interface Slot { kind: 'passkey' | 'password'; at: number; seed: string; h?: string }
/** The ways in, and those removed (id -> when), so that a copy that missed a removal can't bring one back. */
export type Registry = { v: 1; slots: Record<string, Slot>; gone?: Record<string, number> }
export const noSlots = (): Registry => ({ v: 1, slots: {} })
export function mergeRegistry(a: Registry, b: Registry): Registry {
  const gone: Record<string, number> = { ...(a.gone ?? {}) }
  for (const [id, at] of Object.entries(b.gone ?? {})) gone[id] = Math.max(gone[id] ?? 0, at)
  const slots: Record<string, Slot> = {}
  for (const [id, slot] of [...Object.entries(a.slots ?? {}), ...Object.entries(b.slots ?? {})]) {
    if ((gone[id] ?? 0) >= slot.at) continue
    if (!slots[id] || slot.at > slots[id].at) slots[id] = slot
  }
  return { v: 1, slots, ...(Object.keys(gone).length ? { gone } : {}) }
}
