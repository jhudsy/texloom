// Where text selected in the PDF is in the LaTeX source, as best we can: SyncTeX says which lines
// it came from; the selected words are then looked for around those lines. Both sides are reduced
// to plain words first: the source loses its commands, braces, comments and accent macros, the PDF
// text its ligatures, hyphenation and typographic quotes. Words that only one side has (citation
// numbers in the PDF, label names in the source) are allowed for. If too few words match, the
// whole of the lines SyncTeX named is used instead.

export interface Anchor { from: number; to: number; exact: boolean }

interface Word { w: string; from: number; to: number }

const ACCENTS: Record<string, string> = { "'": '́', '`': '̀', '^': '̂', '"': '̈', '~': '̃', '=': '̄', '.': '̇',
  u: '̆', v: '̌', H: '̋', c: '̧', k: '̨', r: '̊', d: '̣', b: '̱' }
const LETTERS: Record<string, string> = { ss: 'ß', o: 'ø', O: 'Ø', ae: 'æ', AE: 'Æ', oe: 'œ', OE: 'Œ', aa: 'å', AA: 'Å', l: 'ł', L: 'Ł', i: 'ı', j: 'ȷ' }

/** A word as both sides compare it: composed, without ligatures, lower case. */
const fold = (s: string) => s.normalize('NFKD').replace(/\p{M}/gu, '').toLowerCase()
// a hyphenated word is one word, without its hyphens: TeX breaks "continuous" as "contin-" and
// "uous", and "over-crediting" can break at its own hyphen; both sides read them the same way
const WORD = /[\p{L}\p{N}]+(?:-[\p{L}\p{N}]+)*/gu
const word = (s: string) => fold(s.replace(/-/g, ''))

/** The words of the PDF's text. */
export function pdfWords(text: string): string[] {
  const plain = text.normalize('NFKC')
    .replace(/­/g, '') // soft hyphens
    .replace(/(\p{L})-\s*\n\s*(\p{L})/gu, '$1$2') // a word hyphenated across lines
  return [...plain.matchAll(WORD)].map((m) => word(m[0]))
}

/** The words of LaTeX source between `start` and `end`, each with where it is in the source. */
export function sourceWords(src: string, start = 0, end = src.length): Word[] {
  // first the text as it would read, keeping each character's place in the source
  const chars: string[] = [], at: number[] = [], until: number[] = []
  const put = (c: string, from: number, to: number) => { chars.push(c); at.push(from); until.push(to) }
  let i = start
  while (i < end) {
    const c = src[i]
    if (c === '%') { while (i < end && src[i] !== '\n') i++; continue } // a comment
    if (c === '\\') {
      const name = /^\\([a-zA-Z]+|.)/.exec(src.slice(i, i + 40))
      if (!name) { i++; continue }
      const n = name[1]
      let j = i + name[0].length
      if (n in ACCENTS && (n.length === 1 && !/[a-zA-Z]/.test(n) || /^[\s{]/.test(src[j] ?? ''))) {
        // an accent on the next letter: \'e, \'{e}, \c c, \c{c}
        while (j < end && /[\s{]/.test(src[j])) j++
        const base = src[j] === '\\' && /^\\[ij]\b/.test(src.slice(j, j + 3)) ? src[j + 1] : src[j]
        if (base && /\p{L}/u.test(base)) {
          j += src[j] === '\\' ? 2 : 1
          if (src[j] === '}') j++
          put((base + ACCENTS[n]).normalize('NFC'), i, j)
          i = j
          continue
        }
      }
      if (n in LETTERS) { // TeX swallows the spaces after \ss, so Stra\ss e reads Straße
        let k = j
        while (k < end && (src[k] === ' ' || src[k] === '\t')) k++
        put(LETTERS[n], i, j); i = k; continue
      }
      if (/^[%&$#_{}]$/.test(n)) { put(n, i, j); i = j; continue } // \% and friends are themselves
      put(' ', i, j) // any other command separates words; its arguments still count
      i = j
      continue
    }
    if (c === '{' || c === '}' || c === '$') { i++; continue } // \textbf{a}b reads "ab"
    if (c === '~') { put(' ', i, i + 1); i++; continue }
    put(c, i, i + 1)
    i++
  }
  const text = chars.join('')
  const words: Word[] = []
  // chars can hold more than one code unit (an accented letter): map positions through them
  const offsets: number[] = []
  let o = 0
  for (let k = 0; k < chars.length; k++) { offsets.push(o); o += chars[k].length }
  const back = (pos: number) => { // index in text -> index in chars
    let lo = 0, hi = offsets.length - 1
    while (lo < hi) { const mid = (lo + hi + 1) >> 1; if (offsets[mid] <= pos) lo = mid; else hi = mid - 1 }
    return lo
  }
  for (const m of text.matchAll(WORD)) {
    const a = back(m.index), b = back(m.index + m[0].length - 1)
    words.push({ w: word(m[0]), from: at[a], to: until[b] }) // (to: past a macro such as \'e or \ss)
  }
  return words
}

/** Start of a line (1-based) in src, and the end of another. */
function lineSpan(src: string, first: number, last: number): [number, number] {
  let a = 0
  for (let n = 1; n < first && a >= 0; n++) a = src.indexOf('\n', a) + 1 || -1
  if (a < 0) a = src.length
  let b = a
  for (let n = first; n <= last; n++) { const e = src.indexOf('\n', b); b = e < 0 ? src.length : e + (n < last ? 1 : 0) }
  return [a, Math.max(a, b)]
}

/** Fit the PDF's words `want` into the source's words `have`: all of want, any stretch of have.
 *  Matching words score; a word only one side has costs a little. The first and last selected
 *  words may be cut short (a selection can start or end inside a word). `near` breaks ties: the
 *  source offset to stay closest to. Null when too few words match. */
function fit(want: string[], have: Word[], near: number): { lo: number; hi: number; matched: number } | null {
  const n = want.length, m = have.length
  if (!n || !m) return null
  const MATCH = 2, SKIP_PDF = -1, SKIP_SRC = -0.5
  const same = (i: number, j: number) => {
    const p = want[i], s = have[j].w
    return p === s || (i === 0 && n > 1 && p.length >= 2 && s.endsWith(p)) || (i === n - 1 && n > 1 && p.length >= 2 && s.startsWith(p))
      || (n === 1 && p.length >= 2 && s.includes(p))
  }
  const W = m + 1
  const score = new Float64Array((n + 1) * W), how = new Uint8Array((n + 1) * W) // 1 match, 2 skip pdf, 3 skip src
  for (let i = 1; i <= n; i++) { score[i * W] = score[(i - 1) * W] + SKIP_PDF; how[i * W] = 2 }
  for (let i = 1; i <= n; i++) {
    for (let j = 1; j <= m; j++) {
      let best = score[(i - 1) * W + j] + SKIP_PDF, h = 2
      const skip = score[i * W + j - 1] + SKIP_SRC
      if (skip > best) { best = skip; h = 3 }
      if (same(i - 1, j - 1)) { const d = score[(i - 1) * W + j - 1] + MATCH; if (d >= best) { best = d; h = 1 } }
      score[i * W + j] = best; how[i * W + j] = h
    }
  }
  let endJ = 0, top = -Infinity
  const dist = (j: number) => Math.abs(have[j - 1].from - near)
  for (let j = 1; j <= m; j++) {
    const s = score[n * W + j]
    if (s > top || (s === top && dist(j) < dist(endJ))) { top = s; endJ = j }
  }
  let i = n, j = endJ, matched = 0, lo = -1, hi = -1
  while (i > 0 && j > 0) {
    const h = how[i * W + j]
    if (h === 1) { matched++; if (hi < 0) hi = j - 1; lo = j - 1; i--; j-- }
    else if (h === 2) i--
    else j--
  }
  // enough of the selection found, in about as many source words as it has
  if (lo < 0 || matched < Math.max(1, Math.ceil(n * 0.6)) || hi - lo + 1 > n * 3 + 10) return null
  return { lo, hi, matched }
}

/** Where `selected` (text from the PDF) is in `src`, near lines `first` to `last` (from SyncTeX). */
export function anchorInSource(src: string, first: number, last: number, selected: string): Anchor {
  if (last < first) [first, last] = [last, first]
  const fallback = (): Anchor => {
    const [a, b] = lineSpan(src, first, last)
    const line = src.slice(a, b)
    const lead = line.length - line.trimStart().length, trail = line.length - line.trimEnd().length
    return { from: a + lead, to: Math.max(a + lead, b - trail), exact: false }
  }
  const want = pdfWords(selected).slice(0, 400)
  const near = lineSpan(src, first, first)[0]
  // the lines around SyncTeX's, more for a longer selection (a paragraph can be many source lines);
  // then far above them too: text in a table or box is put on the line where the box ends
  const slack = 3 + Math.ceil(want.length / 8)
  for (const [above, below] of [[slack, slack], [200, 20]]) {
    const [a] = lineSpan(src, Math.max(1, first - above), Math.max(1, first - above))
    const [, b] = lineSpan(src, last + below, last + below)
    const have = sourceWords(src, a, b).slice(0, 6000)
    const f = fit(want, have, near)
    if (f) return { from: have[f.lo].from, to: have[f.hi].to, exact: true }
  }
  return fallback()
}

/** Text SyncTeX can't place (a bibliography made from a .bib file, say): the file that has it, if
 *  any does. Only a close match counts, since there is no line to fall back on. */
export function findAnywhere(files: { name: string; text: string }[], selected: string): { file: string; from: number; to: number } | null {
  const want = pdfWords(selected).slice(0, 200)
  if (want.length < 3) return null // a word or two could be anywhere
  let best: { file: string; from: number; to: number; matched: number } | null = null
  for (const { name, text } of files) {
    // a cheap look first: does the file have most of the words at all?
    const folded = fold(text.replace(/-/g, ''))
    if (want.filter((w) => folded.includes(w)).length < want.length * 0.6) continue
    const have = sourceWords(text).slice(0, 40000)
    const f = fit(want, have, 0)
    if (f && (!best || f.matched > best.matched)) best = { file: name, from: have[f.lo].from, to: have[f.hi].to, matched: f.matched }
  }
  return best && { file: best.file, from: best.from, to: best.to }
}
