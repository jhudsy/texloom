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

export class Keys {
  private aes?: CryptoKey
  private hmac?: CryptoKey
  constructor(
    readonly project: string, // base64url Ed25519 public key
    readonly key: Uint8Array, // AES-256-GCM key
    readonly seed: Uint8Array | null, // Ed25519 private seed; null => read-only
  ) {}

  static generate(): Keys {
    const seed = ed25519.utils.randomSecretKey()
    return new Keys(b64e(ed25519.getPublicKey(seed)), crypto.getRandomValues(new Uint8Array(32)), seed)
  }

  /** Parse "p=..&k=..&w=.." (a URL fragment, with or without the leading '#'). */
  static fromFragment(frag: string): Keys | null {
    const q = new URLSearchParams(frag.replace(/^#/, ''))
    const p = q.get('p'), k = q.get('k'), w = q.get('w')
    if (!p || !k) return null
    const keys = new Keys(p, b64d(k), w ? b64d(w) : null)
    if (keys.seed && b64e(ed25519.getPublicKey(keys.seed)) !== p) throw new Error('edit key does not match project')
    return keys
  }

  get writable() { return this.seed !== null }

  fragment(write = true): string {
    let f = `p=${this.project}&k=${b64e(this.key)}`
    if (write && this.seed) f += `&w=${b64e(this.seed)}`
    return f
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
    if (!this.seed) throw new Error('read-only link: cannot sign')
    return ed25519.sign(concat(enc.encode(prefix), data), this.seed)
  }

  verify(prefix: string, data: Uint8Array, sig: Uint8Array): boolean {
    try { return ed25519.verify(sig, concat(enc.encode(prefix), data), b64d(this.project)) } catch { return false }
  }

  async blobId(plain: Uint8Array): Promise<string> {
    this.hmac ??= await crypto.subtle.importKey('raw', this.key as BufferSource, { name: 'HMAC', hash: 'SHA-256' }, false, ['sign'])
    const mac = new Uint8Array(await crypto.subtle.sign('HMAC', this.hmac, plain as BufferSource))
    return Array.from(mac.subarray(0, 16), (b) => b.toString(16).padStart(2, '0')).join('')
  }
}
