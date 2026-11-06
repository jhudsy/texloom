// Minimal SyncTeX reader: source line -> PDF position and back.
// Positions are returned in PDF points (bp) measured from the page's top-left corner.

interface Rec { page: number; file: string; line: number; x: number; y: number; w: number; h: number; d: number; container: boolean; i: number }

const SP_PER_BP = 65781.76 // TeX scaled points per PDF point

export class SyncTex {
  private recs: Rec[]
  private constructor(recs: Rec[]) { this.recs = recs }

  static async load(bytes: Uint8Array, files: string[]): Promise<SyncTex | null> {
    let data = bytes
    if (data[0] === 0x1f && data[1] === 0x8b) { // gzip
      const stream = new Blob([data as BlobPart]).stream().pipeThrough(new DecompressionStream('gzip'))
      data = new Uint8Array(await new Response(stream).arrayBuffer())
    }
    return SyncTex.parse(new TextDecoder().decode(data), files)
  }

  static parse(text: string, files: string[]): SyncTex | null {
    const inputs = new Map<string, string>()
    let unit = 1, mag = 1, xo = 0, yo = 0, page = 0
    const recs: Rec[] = []
    for (const line of text.split('\n')) {
      const c = line[0]
      if (c === '{') { page = parseInt(line.slice(1)); continue }
      if (line.startsWith('Input:')) {
        const m = /^Input:(\d+):(.*)$/.exec(line)
        if (m) { const f = project(m[2], files); if (f) inputs.set(m[1], f) }
        continue
      }
      if (line.startsWith('Unit:')) { unit = parseFloat(line.slice(5)) || 1; continue }
      if (line.startsWith('Magnification:')) { mag = (parseFloat(line.slice(14)) || 1000) / 1000; continue }
      if (line.startsWith('X Offset:')) { xo = parseFloat(line.slice(9)) || 0; continue }
      if (line.startsWith('Y Offset:')) { yo = parseFloat(line.slice(9)) || 0; continue }
      if (!page || !'[(hvxkg$'.includes(c)) continue
      const m = /^[[(hvxkg$](\d+),(\d+):(-?\d+),(-?\d+)(?::(-?\d+)(?:,(-?\d+),(-?\d+))?)?/.exec(line)
      if (!m) continue
      const file = inputs.get(m[1])
      if (!file) continue
      const k = (v: string | undefined) => (v ? (parseInt(v) * unit * mag) / SP_PER_BP : 0)
      recs.push({ page, file, line: +m[2], x: k(m[3]) + xo, y: k(m[4]) + yo, w: k(m[5]), h: k(m[6]), d: k(m[7]), container: c === '(' || c === '[', i: recs.length })
    }
    return recs.length ? new SyncTex(recs) : null
  }

  /** Where a source line appears in the PDF (box covering its text). */
  toPdf(file: string, line: number): { page: number; x: number; y: number; w: number; h: number } | null {
    const mine = this.recs.filter((r) => r.file === file && !r.container)
    if (!mine.length) return null
    // the nearest line at or before `line` that produced output (else the next one after)
    const lines = [...new Set(mine.map((r) => r.line))].sort((a, b) => a - b)
    const target = [...lines].reverse().find((l) => l <= line) ?? lines[0]
    const at = mine.filter((r) => r.line === target)
    const page = Math.min(...at.map((r) => r.page))
    const use = at.filter((r) => r.page === page)
    const x = Math.min(...use.map((r) => r.x))
    const right = Math.max(...use.map((r) => r.x + (r.w > 0 ? r.w : 6)))
    const top = Math.min(...use.map((r) => r.y - (r.h > 0 ? r.h : 8)))
    const bottom = Math.max(...use.map((r) => r.y + (r.d > 0 ? r.d : 2)))
    return { page, x, y: top, w: right - x, h: bottom - top }
  }

  /** Which source line produced the PDF point (page, x, y). Glyph/glue records carry
   *  the precise line; paragraph boxes carry the line where the paragraph ended. */
  toSource(page: number, x: number, y: number): { file: string; line: number } | null {
    const onPage = this.recs.filter((r) => r.page === page && !r.container)
    if (!onPage.length) return null
    const vdist = (r: Rec) => {
      const top = r.y - (r.h > 0 ? r.h : 9), bottom = r.y + (r.d > 0 ? r.d : 3)
      return y < top ? top - y : y > bottom ? y - bottom : 0
    }
    const minV = Math.min(...onPage.map(vdist))
    const row = onPage.filter((r) => vdist(r) <= minV + 0.5)
    // the text run the click falls in: the last record starting at or before x ...
    const before = row.filter((r) => r.x <= x + 0.5).sort((a, b) => b.x - a.x || b.i - a.i) // ties: the later record (text after a space)
    if (before.length) return { file: before[0].file, line: before[0].line }
    // ... else the nearest one to the right
    const after = row.sort((a, b) => a.x - b.x)[0]
    return { file: after.file, line: after.line }
  }
}

function project(path: string, files: string[]): string | null {
  const p = path.replace(/\/\.\//g, '/').replace(/^\.\//, '')
  if (files.includes(p)) return p
  return files.find((f) => p.endsWith('/' + f)) ?? null
}
