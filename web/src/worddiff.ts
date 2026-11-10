// Differences between two pieces of text, word by word: what a suggested change shows (struck
// words and new words) and what accepting one changes. No DOM here, so scripts/check-suggest.mjs
// can test it.

export type DiffOp = { op: 'eq' | 'del' | 'ins'; text: string }

// Chinese and Japanese are written without spaces, so each character counts as a word.
const CJK = '\\p{Script=Han}\\p{Script=Hiragana}\\p{Script=Katakana}'
const TOKEN = new RegExp(`[${CJK}]|\\\\[A-Za-z@]+|[\\p{L}\\p{M}\\p{N}_]+|\\s+|[\\s\\S]`, 'gu')
const WORD_CHAR = /[\p{L}\p{M}\p{N}_]/u
const CJK_CHAR = new RegExp(`[${CJK}]`, 'u')

/** A character that's part of a word (for keeping changes to whole words). */
export const isWordChar = (c: string | undefined): boolean => !!c && WORD_CHAR.test(c) && !CJK_CHAR.test(c)

export const tokens = (s: string): string[] => s.match(TOKEN) ?? []

const MAX_D = 600 // beyond this many differing words, show it as one replacement

/** The shortest edit script between two token lists (Myers), or null if it's too long. */
function myers(a: string[], b: string[]): ('eq' | 'del' | 'ins')[] | null {
  const n = a.length, m = b.length, max = Math.min(n + m, MAX_D)
  const off = max + 1
  const v = new Int32Array(2 * max + 3)
  const trace: Int32Array[] = []
  for (let d = 0; d <= max; d++) {
    trace.push(v.slice(off - d - 1, off + d + 2))
    for (let k = -d; k <= d; k += 2) {
      let x = k === -d || (k !== d && v[off + k - 1] < v[off + k + 1]) ? v[off + k + 1] : v[off + k - 1] + 1
      let y = x - k
      while (x < n && y < m && a[x] === b[y]) { x++; y++ }
      v[off + k] = x
      if (x >= n && y >= m) return backtrack(trace, n, m, d)
    }
  }
  return null
}

function backtrack(trace: Int32Array[], n: number, m: number, dEnd: number): ('eq' | 'del' | 'ins')[] {
  const out: ('eq' | 'del' | 'ins')[] = []
  let x = n, y = m
  for (let d = dEnd; d > 0; d--) {
    const v = trace[d] // holds k from -d-1 to d+1, at index k + d + 1
    const at = (k: number) => v[k + d + 1]
    const k = x - y
    const prevK = k === -d || (k !== d && at(k - 1) < at(k + 1)) ? k + 1 : k - 1
    const prevX = at(prevK), prevY = prevX - prevK
    while (x > prevX && y > prevY) { out.push('eq'); x--; y-- }
    out.push(x === prevX ? 'ins' : 'del')
    x = prevX; y = prevY
  }
  while (x > 0 && y > 0) { out.push('eq'); x--; y-- }
  return out.reverse()
}

/** How `before` became `after`, word by word. Each change is its deletion then its insertion,
 *  and spaces or single punctuation between two changes join them, so it reads as phrases. */
export function wordDiff(before: string, after: string): DiffOp[] {
  if (before === after) return before ? [{ op: 'eq', text: before }] : []
  const a = tokens(before), b = tokens(after)
  let p = 0
  while (p < a.length && p < b.length && a[p] === b[p]) p++
  let q = 0
  while (q < a.length - p && q < b.length - p && a[a.length - 1 - q] === b[b.length - 1 - q]) q++
  const midA = a.slice(p, a.length - q), midB = b.slice(p, b.length - q)
  const script = myers(midA, midB) ?? [...midA.map(() => 'del' as const), ...midB.map(() => 'ins' as const)]

  // walk the script, collecting changes; a short stretch in common between changes joins them
  type Block = { eq: string } | { del: string; ins: string }
  const blocks: Block[] = []
  const prefix = a.slice(0, p).join('')
  if (prefix) blocks.push({ eq: prefix })
  let i = 0, j = 0
  for (let s = 0; s < script.length;) {
    if (script[s] === 'eq') {
      let text = ''
      while (s < script.length && script[s] === 'eq') { text += midA[i++]; j++; s++ }
      blocks.push({ eq: text })
    } else {
      let del = '', ins = ''
      while (s < script.length && script[s] !== 'eq') {
        if (script[s] === 'del') del += midA[i++]; else ins += midB[j++]
        s++
      }
      blocks.push({ del, ins })
    }
  }
  const suffix = a.slice(a.length - q).join('')
  if (suffix) blocks.push({ eq: suffix })

  const joined: Block[] = []
  for (const blk of blocks) {
    const prev = joined[joined.length - 1], before2 = joined[joined.length - 2]
    if ('del' in blk && prev && 'eq' in prev && before2 && 'del' in before2 && isGlue(prev.eq)) {
      joined.pop()
      before2.del += prev.eq + blk.del
      before2.ins += prev.eq + blk.ins
    } else if ('del' in blk && prev && 'del' in prev) {
      prev.del += blk.del; prev.ins += blk.ins
    } else if ('eq' in blk && prev && 'eq' in prev) prev.eq += blk.eq
    else joined.push({ ...blk })
  }
  const out: DiffOp[] = []
  for (const blk of joined) {
    if ('eq' in blk) out.push({ op: 'eq', text: blk.eq })
    else {
      if (blk.del) out.push({ op: 'del', text: blk.del })
      if (blk.ins) out.push({ op: 'ins', text: blk.ins })
    }
  }
  return out
}

/** Whitespace without a line break, or one punctuation mark: too little to stand between two changes. */
const isGlue = (s: string) => (/^[^\S\n]+$/.test(s) || (s.length === 1 && !isWordChar(s) && !/\s/.test(s)))
