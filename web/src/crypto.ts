// Keys, links, encryption and signing. Must match texloom/crypto.py exactly.
import { ed25519 } from '@noble/curves/ed25519.js'

export const b64e = (b: Uint8Array): string => {
  let s = ''
  for (let i = 0; i < b.length; i += 0x8000) s += String.fromCharCode(...b.subarray(i, i + 0x8000))
  return btoa(s).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
}
export const b64d = (s: string): Uint8Array => {
  const bin = atob(s.replace(/-/g, '+').replace(/_/g, '/') + '='.repeat((4 - (s.length % 4)) % 4))
  const out = new Uint8Array(bin.length)
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i)
  return out
}
const enc = new TextEncoder()
const concat = (a: Uint8Array, b: Uint8Array) => {
  const o = new Uint8Array(a.length + b.length)
  o.set(a); o.set(b, a.length)
  return o
}

// Personal edit links: the owner (holder of the project's signing key) grants a person
// their own key. Their signatures carry the grant, so anyone can check them against the
// project id alone; the owner can later revoke a key (the relay then refuses it).
//   member signature = 'M' | person's public key (32) | owner's grant signature (64)
//                      | name length (1) | name (UTF-8) | signature (64)
export interface Member { seed: Uint8Array; grant: Uint8Array; name: string }
const MEMBER_TAG = 0x4d // keep in sync with texloom/crypto.py
// Strict verification (no ZIP-215 leniency), as OpenSSL does in the relay and the helper,
// so every party accepts exactly the same signatures.
const STRICT = { zip215: false }
const verifySig = (sig: Uint8Array, msg: Uint8Array, pub: Uint8Array) => ed25519.verify(sig, msg, pub, STRICT)

/** The one spelling of a 32-byte public key, or null (base64 decoders accept variants). */
export function canonicalPub(p: unknown): string | null {
  if (typeof p !== 'string' || p.length > 64) return null
  try { const raw = b64d(p); return raw.length === 32 ? b64e(raw) : null } catch { return null }
}
const grantMsg = (pub: Uint8Array, name: string) => concat(concat(enc.encode('grant:'), pub), enc.encode(`:${name}`))
export const revokeMsg = (pub: string) => concat(enc.encode('revoke:'), b64d(pub))

/** Who signed: 'owner', a person's public key and name, or null if the signature is invalid. */
export function signer(project: string, msg: Uint8Array, sig: Uint8Array): { owner: true } | { pub: string; name: string } | null {
  try {
    const root = b64d(project)
    if (sig.length === 64) return verifySig(sig, msg, root) ? { owner: true } : null
    if (sig[0] !== MEMBER_TAG || sig.length < 162) return null
    const pub = sig.subarray(1, 33), grant = sig.subarray(33, 97), n = sig[97]
    if (sig.length !== 98 + n + 64) return null
    // ignoreBOM: keep a leading BOM in the name, as Python does
    const name = new TextDecoder('utf-8', { fatal: true, ignoreBOM: true }).decode(sig.subarray(98, 98 + n))
    if (!verifySig(grant, grantMsg(pub, name), root)) return null
    if (!verifySig(sig.subarray(98 + n), msg, pub)) return null
    return { pub: b64e(pub), name }
  } catch { return null }
}

export class Keys {
  private aes?: CryptoKey
  private hmac?: CryptoKey
  /** Revoked personal keys (canonical base64url), learned from the relay; their peer-to-peer updates are refused. */
  readonly revoked = new Set<string>()
  /** Personal keys seen signing valid data in this project: pub -> name (a member can't hide from the owner). */
  readonly signers = new Map<string, string>()
  constructor(
    readonly project: string, // base64url Ed25519 public key
    readonly key: Uint8Array, // AES-256-GCM key
    readonly seed: Uint8Array | null, // owner's Ed25519 private seed; null => not the owner
    readonly member: Member | null = null, // a personal edit link
  ) {}

  static generate(): Keys {
    const seed = ed25519.utils.randomSecretKey()
    return new Keys(b64e(ed25519.getPublicKey(seed)), crypto.getRandomValues(new Uint8Array(32)), seed)
  }

  /** Parse "p=..&k=..&w=.." (a URL fragment, with or without the leading '#'). */
  static fromFragment(frag: string): Keys | null {
    const q = new URLSearchParams(frag.replace(/^#/, ''))
    const p = q.get('p'), k = q.get('k'), w = q.get('w'), m = q.get('m'), g = q.get('g'), n = q.get('n')
    if (!p || !k) return null
    let member: Member | null = null
    if (m && g && n !== null) {
      member = { seed: b64d(m), grant: b64d(g), name: n }
      if (!verifySig(member.grant, grantMsg(ed25519.getPublicKey(member.seed), n), b64d(p))) throw new Error('this personal link was not issued for this project')
    }
    const keys = new Keys(p, b64d(k), w ? b64d(w) : null, member)
    if (keys.seed && b64e(ed25519.getPublicKey(keys.seed)) !== p) throw new Error('edit key does not match project')
    return keys
  }

  get writable() { return this.seed !== null || this.member !== null }
  /** The owner can issue and revoke personal links. */
  get owner() { return this.seed !== null }
  /** This link's personal key (base64url), if it is a personal link. */
  get memberPub(): string | null { return this.member ? b64e(ed25519.getPublicKey(this.member.seed)) : null }

  fragment(write = true): string {
    let f = `p=${this.project}&k=${b64e(this.key)}`
    if (write && this.seed) f += `&w=${b64e(this.seed)}`
    else if (write && this.member) f += `&m=${b64e(this.member.seed)}&g=${b64e(this.member.grant)}&n=${encodeURIComponent(this.member.name)}`
    return f
  }

  /** Owner only: a personal edit link for `name`. */
  personal(name: string): { pub: string; grant: string; keys: Keys } {
    if (!this.seed) throw new Error('only the owner can create personal links')
    const clean = name.trim().slice(0, 60) || 'collaborator'
    if (enc.encode(clean).length > 200) throw new Error('name too long')
    const mseed = ed25519.utils.randomSecretKey()
    const pub = ed25519.getPublicKey(mseed)
    const grant = ed25519.sign(grantMsg(pub, clean), this.seed)
    return { pub: b64e(pub), grant: b64e(grant), keys: new Keys(this.project, this.key, null, { seed: mseed, grant, name: clean }) }
  }

  /** Owner only: the signed revocation of a personal key, for its canonical spelling. */
  revocation(pub: string): { p: string; s: string } {
    if (!this.seed) throw new Error('only the owner can revoke links')
    const p = canonicalPub(pub)
    if (!p) throw new Error('not a personal key')
    return { p, s: b64e(ed25519.sign(revokeMsg(p), this.seed)) }
  }

  /** Was this grant issued by the owner, for this key and name? (Entries in `members` can be edited by anyone.) */
  grantValid(pub: string, name: string, grant: unknown): boolean {
    try { return typeof grant === 'string' && verifySig(b64d(grant), grantMsg(b64d(pub), name), b64d(this.project)) } catch { return false }
  }

  /** Owner-signed values (e.g. “this project moved”), which editors can't fake. */
  ownerSign(text: string): string {
    if (!this.seed) throw new Error('owner only')
    return b64e(ed25519.sign(enc.encode(`owner:${text}`), this.seed))
  }
  ownerSigned(text: string, sig: unknown): boolean {
    try { return typeof sig === 'string' && verifySig(b64d(sig), enc.encode(`owner:${text}`), b64d(this.project)) } catch { return false }
  }

  link(origin: string, write = true) { return `${origin}/#${this.fragment(write)}` }

  private async aesKey() {
    return (this.aes ??= await crypto.subtle.importKey('raw', this.key as BufferSource, 'AES-GCM', false, ['encrypt', 'decrypt']))
  }

  async encrypt(plain: Uint8Array): Promise<Uint8Array> {
    const iv = crypto.getRandomValues(new Uint8Array(12))
    const ct = await crypto.subtle.encrypt({ name: 'AES-GCM', iv }, await this.aesKey(), plain as BufferSource)
    return concat(iv, new Uint8Array(ct))
  }

  async decrypt(data: Uint8Array): Promise<Uint8Array> {
    const pt = await crypto.subtle.decrypt({ name: 'AES-GCM', iv: data.subarray(0, 12) as BufferSource }, await this.aesKey(), data.subarray(12) as BufferSource)
    return new Uint8Array(pt)
  }

  sign(prefix: string, data: Uint8Array): Uint8Array {
    const msg = concat(enc.encode(prefix), data)
    if (this.seed) return ed25519.sign(msg, this.seed)
    if (!this.member) throw new Error('read-only link: cannot sign')
    const m = this.member, name = enc.encode(m.name)
    const out = new Uint8Array(98 + name.length + 64)
    out[0] = MEMBER_TAG
    out.set(ed25519.getPublicKey(m.seed), 1); out.set(m.grant, 33); out[97] = name.length; out.set(name, 98)
    out.set(ed25519.sign(msg, m.seed), 98 + name.length)
    return out
  }

  /** Valid signature by the owner or a personal key. `fresh`: also refuse revoked keys
   *  (for new peer-to-peer updates; the relay already refuses them for stored ones). */
  verify(prefix: string, data: Uint8Array, sig: Uint8Array, fresh = false): boolean {
    const who = signer(this.project, concat(enc.encode(prefix), data), sig)
    if (who && 'pub' in who && !this.signers.has(who.pub)) this.signers.set(who.pub, who.name)
    return !!who && !(fresh && 'pub' in who && this.revoked.has(who.pub))
  }

  async blobId(plain: Uint8Array): Promise<string> {
    this.hmac ??= await crypto.subtle.importKey('raw', this.key as BufferSource, { name: 'HMAC', hash: 'SHA-256' }, false, ['sign'])
    const mac = new Uint8Array(await crypto.subtle.sign('HMAC', this.hmac, plain as BufferSource))
    return Array.from(mac.subarray(0, 16), (b) => b.toString(16).padStart(2, '0')).join('')
  }
}
