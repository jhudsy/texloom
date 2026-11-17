// Compressed document updates: what a snapshot (or a big change) holds once compressed.
//
// A packed update is MARKER followed by the update compressed with zlib (deflate, with its
// checksum), and it's encrypted like any other. Encrypted data doesn't compress, so this happens
// before. Anything else is a plain update. Data counts as packed only if it has the marker and
// decompresses cleanly (zlib checks it), so a plain update can't be taken for one.
//
// Every app reads packed updates before any writes them (ROADMAP.md, issue #18). Keep in step with
// texloom/packing.py; scripts/packing-vectors.json checks both.

export const MARKER = new Uint8Array([0x00, 0x54, 0x4c, 0x7a]) // "\0TLz"
const MAX_UNPACKED = 256 * 1024 * 1024 // an update this large would be refused anyway; stops a decompression bomb

export const isPacked = (data: Uint8Array) => data.length > MARKER.length && MARKER.every((b, i) => data[i] === b)

async function through(data: Uint8Array, stream: CompressionStream | DecompressionStream, limit = Infinity): Promise<Uint8Array> {
  const reader = new Blob([data as BlobPart]).stream().pipeThrough(stream).getReader()
  const parts: Uint8Array[] = []
  let size = 0
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    size += value.length
    if (size > limit) { await reader.cancel(); throw new Error('too large') }
    parts.push(value)
  }
  const out = new Uint8Array(size)
  let at = 0
  for (const p of parts) { out.set(p, at); at += p.length }
  return out
}

export async function pack(update: Uint8Array): Promise<Uint8Array> {
  const z = await through(update, new CompressionStream('deflate'))
  const out = new Uint8Array(MARKER.length + z.length)
  out.set(MARKER)
  out.set(z, MARKER.length)
  return out
}

/** The update itself: decompressed if it's packed, else as it is. */
export async function unpack(data: Uint8Array): Promise<Uint8Array> {
  if (!isPacked(data)) return data
  try {
    return await through(data.subarray(MARKER.length), new DecompressionStream('deflate'), MAX_UNPACKED)
  } catch {
    return data // cut short, too large, or not zlib: not one of ours
  }
}
