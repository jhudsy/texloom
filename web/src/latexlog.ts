// Parse a TeX/LaTeX log into problems (errors, warnings, bad boxes) with project
// file and line, for a clickable Problems list and editor markers.

export interface Problem {
  severity: 'error' | 'warning' | 'box'
  file: string | null // project path, or null if not in the project / unknown
  line: number | null
  message: string
}

/** Map a path from the log to a project path (or null if it's a system file). */
function toProject(path: string | null, files: string[]): string | null {
  if (!path) return null
  const p = path.replace(/\/\.\//g, '/').replace(/^\.\//, '')
  if (files.includes(p)) return p
  return files.find((f) => p.endsWith('/' + f)) ?? files.find((f) => p.endsWith('/' + f.replace(/\.tex$/, '')) || p === f.replace(/\.tex$/, '')) ?? null
}

/** The project file a .bib path from biber means: relative to the main file, which may be
 *  in a folder ("bib/own.bib" is "paper/bib/own.bib"); case-insensitively as a fallback. */
function bibFile(path: string, files: string[]): string | null {
  const p = path.split('/').filter((s) => s && s !== '.' && s !== '..').join('/')
  const ends = (f: string, t: string) => f === t || f.endsWith('/' + t)
  return files.find((f) => ends(f, p)) ?? files.find((f) => ends(f.toLowerCase(), p.toLowerCase())) ?? null
}

export function parseLog(log: string, files: string[]): Problem[] {
  const lines = log.split('\n')
  const out: Problem[] = []
  const seen = new Set<string>()
  const add = (p: Problem) => {
    const key = `${p.severity}|${p.file}|${p.line}|${p.message}`
    if (!seen.has(key)) { seen.add(key); out.push(p) }
  }
  const stack: (string | null)[] = [] // open "(file" groups; null for ordinary parentheses
  let bibSource: string | null = null // the .bib biber is reading ("Found BibTeX data source 'bib/own.bib'")
  const bibSources = new Set<string>() // every .bib biber looked for
  const current = () => { for (let i = stack.length - 1; i >= 0; i--) if (stack[i]) return stack[i]; return null }

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i]
    const fileAtStart = current()

    // -file-line-error style: ./main.tex:12: message
    const fle = /^(\.?\/?[^:\s][^:]*\.(?:tex|sty|cls|bib|ltx|bbl)):(\d+): (.*)$/.exec(line)
    if (/==> Fatal error occurred|^! Emergency stop/.test(line)) { /* summary noise, not a problem */ }
    else if (fle) add({ severity: 'error', file: toProject(fle[1], files), line: +fle[2], message: fle[3].trim() })
    else if (line.startsWith('! ')) {
      let lineNo: number | null = null
      for (let j = i + 1; j < Math.min(lines.length, i + 12); j++) {
        const m = /^l\.(\d+)/.exec(lines[j])
        if (m) { lineNo = +m[1]; break }
      }
      add({ severity: 'error', file: toProject(fileAtStart, files), line: lineNo, message: line.slice(2).trim() })
    }

    const warn = /^(?:LaTeX|Package (\S+)|Class (\S+)) Warning: (.*)$/.exec(line)
    if (warn) {
      let msg = warn[3]
      for (let j = i + 1; j < lines.length && lines[j].trim() && j < i + 6; j++) msg += ' ' + lines[j].replace(/^\(\S+\)\s*/, '').trim()
      const at = /on input line (\d+)/.exec(msg)
      add({ severity: 'warning', file: at ? toProject(fileAtStart, files) : null, line: at ? +at[1] : null,
        message: `${warn[1] ?? warn[2] ? `${warn[1] ?? warn[2]}: ` : ''}${msg.replace(/\s+/g, ' ').trim()}` })
    }

    // biber ("ERROR - Cannot find 'refs.bib'!") and BibTeX ("I couldn't open database file refs.bib")
    const source = /Found BibTeX data source '([^']+)'/.exec(line)
    if (source) bibSource = source[1]
    const looking = /Looking for bibtex file '([^']+)'/.exec(line)
    if (looking) bibSources.add(looking[1])
    const biber = /\b(ERROR|WARN) - (.*)$/.exec(line)
    if (biber) {
      // a syntax error is reported in biber's UTF-8 copy of the .bib ("/tmp/biber_tmp_…utf8,
      // line 130, …"): name the project's file and line instead, so it can be clicked
      const sub = /^BibTeX subsystem: (\S+), line (\d+), (.*)$/.exec(biber[2].trim())
      // biber names its copy after the MD5 of the file's name: that says which .bib it is
      // (warnings can come after it has moved on to the next one)
      const hash = sub ? /([0-9a-f]{32})_\d+\.utf8,?$/.exec(sub[1])?.[1] : undefined
      const named = hash ? [...bibSources].find((p) => md5(p.slice(p.lastIndexOf('/') + 1)) === hash) : undefined
      const src = named ?? bibSource
      const bib = sub && src ? bibFile(src, files) : null
      add(sub && src
        ? { severity: biber[1] === 'ERROR' ? 'error' : 'warning', file: bib, line: +sub[2], message: `Biber: ${src}, line ${sub[2]}: ${sub[3]}` }
        : { severity: biber[1] === 'ERROR' ? 'error' : 'warning', file: null, line: null, message: `Biber: ${biber[2].trim()}` })
    }
    const bibtex = /^I (couldn't open (?:database|style|auxiliary) file .*|found no \\\w+ commands?.*)$/.exec(line)
    if (bibtex) add({ severity: 'error', file: null, line: null, message: `BibTeX: I ${bibtex[1].replace(/---.*$/, '').trim()}` })
    const bibwarn = /^Warning--(.*)$/.exec(line)
    if (bibwarn) add({ severity: 'warning', file: null, line: null, message: `BibTeX: ${bibwarn[1].trim()}` })

    const box = /^((?:Over|Under)full \\[hv]box \([^)]*\)).*?(?:at lines? (\d+)|detected at line (\d+))/.exec(line)
    if (box) add({ severity: 'box', file: toProject(fileAtStart, files), line: +(box[2] ?? box[3]), message: box[1] })

    // track which file the following lines belong to: "(path" opens, ")" closes
    for (let c = 0; c < line.length; c++) {
      const ch = line[c]
      if (ch === '(') {
        const m = /^\(([^\s()[\]{}"]+\.[A-Za-z0-9]+)/.exec(line.slice(c))
        stack.push(m && !/^\d/.test(m[1]) ? m[1] : null)
      } else if (ch === ')') stack.pop()
    }
  }
  const rank = { error: 0, warning: 1, box: 2 }
  return out.sort((a, b) => rank[a.severity] - rank[b.severity])
}

/** MD5 of a string's UTF-8 bytes, as hex (biber names its temporary copies with it). */
export function md5(text: string): string {
  const data = new TextEncoder().encode(text)
  const n = ((data.length + 8) >> 6) + 1
  const words = new Uint32Array(n * 16)
  for (let i = 0; i < data.length; i++) words[i >> 2] |= data[i] << ((i % 4) * 8)
  words[data.length >> 2] |= 0x80 << ((data.length % 4) * 8)
  words[n * 16 - 2] = data.length * 8
  const K = Array.from({ length: 64 }, (_, i) => Math.floor(Math.abs(Math.sin(i + 1)) * 2 ** 32) >>> 0)
  const S = [7, 12, 17, 22, 5, 9, 14, 20, 4, 11, 16, 23, 6, 10, 15, 21]
  let a0 = 0x67452301, b0 = 0xefcdab89, c0 = 0x98badcfe, d0 = 0x10325476
  for (let blk = 0; blk < n; blk++) {
    let a = a0, b = b0, c = c0, d = d0
    for (let i = 0; i < 64; i++) {
      const r = i >> 4
      let f: number, g: number
      if (r === 0) { f = (b & c) | (~b & d); g = i }
      else if (r === 1) { f = (d & b) | (~d & c); g = (5 * i + 1) % 16 }
      else if (r === 2) { f = b ^ c ^ d; g = (3 * i + 5) % 16 }
      else { f = c ^ (b | ~d); g = (7 * i) % 16 }
      const tmp = d; d = c; c = b
      const x = (a + f + K[i] + words[blk * 16 + g]) >>> 0
      const sh = S[r * 4 + (i % 4)]
      b = (b + ((x << sh) | (x >>> (32 - sh)))) >>> 0
      a = tmp
    }
    a0 = (a0 + a) >>> 0; b0 = (b0 + b) >>> 0; c0 = (c0 + c) >>> 0; d0 = (d0 + d) >>> 0
  }
  return [a0, b0, c0, d0].map((v) => [0, 8, 16, 24].map((s) => ((v >>> s) & 255).toString(16).padStart(2, '0')).join('')).join('')
}
