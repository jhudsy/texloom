// The parts of the project list that must match texloom/listsync.py exactly: keys, merging and the
// password function. Nothing here needs a browser, so scripts/check-listsync.mjs can test it
// against scripts/listsync-vectors.json, as scripts/check_listsync.py does for the Python.
import { ed25519 } from '@noble/curves/ed25519.js'
import { scryptAsync } from '@noble/hashes/scrypt.js'
import { b64e } from './crypto'

export interface ListEntry { f: string; n: string; at: number; w: boolean; nick?: string; t?: string; by?: string }
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

export function forgetOldRemovals(doc: ListDoc): ListDoc {
  const cutoff = Date.now() - YEAR
  return { ...doc, gone: Object.fromEntries(Object.entries(doc.gone).filter(([, when]) => when >= cutoff)) }
}

/** What a username and password stand for. Slow on purpose, and done on this device: neither is
 *  ever sent anywhere. */
export const passwordSecret = (username: string, password: string) => scryptAsync(
  enc.encode(password.normalize('NFKC')),
  enc.encode(`texloom list password v1:${username.trim().toLowerCase().normalize('NFKC')}`),
  { N: 2 ** 17, r: 8, p: 1, dkLen: 32, maxmem: 256 * 1024 * 1024 + 1024 },
)
