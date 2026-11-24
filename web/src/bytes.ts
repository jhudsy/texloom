// Bytes as text, and hashes: hex and plain base64 (crypto.ts has the URL-safe base64 links and
// signatures use). No imports and no DOM, so the node checks can bundle anything that uses it.

/** Lowercase hex, two digits a byte. */
export const hex = (d: Uint8Array | ArrayBuffer): string => Array.from(d instanceof Uint8Array ? d : new Uint8Array(d), (b) => b.toString(16).padStart(2, '0')).join('')

/** `n` random bytes, as hex. */
export const randomHex = (n: number): string => hex(crypto.getRandomValues(new Uint8Array(n)))

/** A digest ('SHA-1', 'SHA-256') of `d`. */
export const digest = async (alg: 'SHA-1' | 'SHA-256', d: Uint8Array): Promise<Uint8Array> =>
  new Uint8Array(await crypto.subtle.digest(alg, d as BufferSource))

/** Standard base64, with padding (in pieces: spreading a big array into one call would overflow the stack). */
export const b64 = (d: Uint8Array): string => {
  let s = ''
  for (let i = 0; i < d.length; i += 0x8000) s += String.fromCharCode(...d.subarray(i, i + 0x8000))
  return btoa(s)
}

/** Bytes from standard base64 (whitespace, as in a JSON API's line-wrapped content, is ignored). */
export const unb64 = (s: string): Uint8Array => Uint8Array.from(atob(s.replace(/\s/g, '')), (c) => c.charCodeAt(0))
