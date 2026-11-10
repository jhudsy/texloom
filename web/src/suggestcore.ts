// Suggest mode's bookkeeping, with no editor or DOM, so scripts/check-suggest.mjs can test it.
//
// While suggesting, the shared file never changes. The editor shows "your view": the shared text
// with your open suggestions applied. Each suggestion is a hunk, a stretch [a, b) of the shared
// text and what you'd put there. A hunk is stored as an ordinary suggestion in the review map
// (quote = the shared text, text = the replacement), so the Review tab, accepting and rejecting,
// the assistant and the MCP tools all treat it like any other suggestion.
//
// Hunks are kept to whole words, and a pure insertion takes in the word next to it, so a
// suggestion reads "is → is very" rather than "insert ' very' here". Hunks never touch: two that
// would are one.
import * as Y from 'yjs'
import { isWordChar, wordDiff } from './worddiff'

export interface Hunk {
  id: string | null // the review item it's saved as; null until saved
  a: number // where it starts and ends in the shared text
  b: number
  text: string // what goes there instead
  /** The shared text it replaces, as this tab last saw it: when others change it, refresh()
   *  carries their change into `text` if it doesn't touch the words being changed. */
  base?: string
  /** Someone changed the very words this replaces: accepting it asks first. */
  conflict?: boolean
  // where it is, held while unsaved so that others' edits move it (see anchor() and refresh())
  s?: Y.RelativePosition
  e?: Y.RelativePosition
}

const MAX_GROW = 40 // characters a hunk grows by to reach whole words

/** The shared text with the hunks applied: what the editor shows. Hunks are sorted and apart. */
export function compose(shared: string, hunks: readonly Hunk[]): string {
  let out = '', at = 0
  for (const h of hunks) { out += shared.slice(at, h.a) + h.text; at = h.b }
  return out + shared.slice(at)
}

/** Where shared position `x` is in the view. Inside a hunk's replaced text it's the hunk's start. */
export function toView(hunks: readonly Hunk[], x: number): number {
  let d = 0
  for (const h of hunks) {
    if (x <= h.a) return x + d
    if (x < h.b) return h.a + d
    d += h.text.length - (h.b - h.a)
  }
  return x + d
}

/** Where view position `v` is in the shared text. Inside a hunk's new text, or where a deletion
 *  was, its start (bias < 0) or end. */
export function toShared(hunks: readonly Hunk[], v: number, bias: -1 | 1 = -1): number {
  let d = 0
  for (const h of hunks) {
    const vs = h.a + d, ve = vs + h.text.length
    if (v < vs) return v - d
    if (v === vs && ve > vs) return h.a
    if (v < ve || v === vs) return bias < 0 ? h.a : h.b
    if (v === ve) return h.b
    d += h.text.length - (h.b - h.a)
  }
  return v - d
}

const high = (c: number) => c >= 0xd800 && c <= 0xdbff
const low = (c: number) => c >= 0xdc00 && c <= 0xdfff

/** Tidy one hunk: drop what it has in common with the shared text, then grow it back to whole
 *  words, staying within [min, max) (the neighbouring hunks). Null if it changes nothing. */
export function normalize(shared: string, a: number, b: number, text: string, id: string | null, min = 0, max = shared.length): Hunk | null {
  const base = shared.slice(a, b)
  const n = Math.min(base.length, text.length)
  let p = 0
  while (p < n && base.charCodeAt(p) === text.charCodeAt(p)) p++
  if (p > 0 && high(base.charCodeAt(p - 1))) p-- // don't split a character in two
  let q = 0
  while (q < n - p && base.charCodeAt(base.length - 1 - q) === text.charCodeAt(text.length - 1 - q)) q++
  if (q > 0 && low(base.charCodeAt(base.length - q))) q--
  a += p; b -= q; text = text.slice(p, text.length - q)
  if (a === b && !text) return null

  // to whole words: a hunk that starts or ends inside a word takes in the rest of it
  const splitsLeft = () => isWordChar(shared[a - 1]) && ((a < b && isWordChar(shared[a])) || isWordChar(text[0]))
  const splitsRight = () => isWordChar(shared[b]) && ((a < b && isWordChar(shared[b - 1])) || isWordChar(text[text.length - 1]))
  for (let g = 0; a > min && g < MAX_GROW && splitsLeft(); g++) { a--; text = shared[a] + text }
  for (let g = 0; b < max && g < MAX_GROW && splitsRight(); g++) { text += shared[b]; b++ }

  // something to read: an insertion takes in the word beside it, and a change to spaces or
  // punctuation alone takes in the words on both sides
  const wordy = (s: string) => [...s].some((c) => isWordChar(c))
  const growLeft = () => { let g = 0; while (a > min && g < MAX_GROW && isWordChar(shared[a - 1])) { a--; text = shared[a] + text; g++ } return g > 0 }
  const growRight = () => { let g = 0; while (b < max && g < MAX_GROW && isWordChar(shared[b])) { text += shared[b]; b++; g++ } return g > 0 }
  if (!wordy(shared.slice(a, b)) && !wordy(text)) { growLeft(); growRight() }
  else if (a === b && !growLeft()) growRight()
  return { id, a, b, text, base: shared.slice(a, b) }
}

/** View positions of each hunk's new text. */
function spans(hunks: readonly Hunk[]): { vs: number; ve: number }[] {
  let d = 0
  return hunks.map((h) => {
    const vs = h.a + d
    d += h.text.length - (h.b - h.a)
    return { vs, ve: vs + h.text.length }
  })
}

/**
 * The hunks after one edit in the view: `view` (before the edit) had [f, t) replaced by `s`.
 * Hunks the edit touches become one, with the first one's id. Returns the new hunks and the ids
 * of the hunks that went (merged in, or edited back to the shared text).
 */
export function edit(shared: string, hunks: readonly Hunk[], view: string, f: number, t: number, s: string): { hunks: Hunk[]; gone: string[] } {
  const sp = spans(hunks)
  const touched = hunks.map((_, i) => i).filter((i) => sp[i].ve >= f && sp[i].vs <= t)
  const rs = Math.min(f, ...touched.map((i) => sp[i].vs)), re = Math.max(t, ...touched.map((i) => sp[i].ve))
  const sa = toShared(hunks, rs, -1), sb = toShared(hunks, re, 1)
  const region = view.slice(rs, f) + s + view.slice(t, re)
  const ids = touched.map((i) => hunks[i].id).filter((x): x is string => !!x)
  const rest = hunks.filter((_, i) => !touched.includes(i))
  const before = rest.filter((h) => h.b <= sa), after = rest.filter((h) => h.a >= sb)
  const min = before.length ? before[before.length - 1].b : 0, max = after.length ? after[0].a : shared.length
  const h = normalize(shared, sa, sb, region, ids[0] ?? null, min, max)
  if (h && touched.some((i) => hunks[i].conflict)) h.conflict = true
  const merged = settle(shared, h ? [...before, h, ...after] : [...before, ...after])
  const kept = new Set(merged.hunks.map((x) => x.id))
  return { hunks: merged.hunks, gone: [...new Set([...ids, ...merged.gone])].filter((id) => !kept.has(id)) }
}

/** Sort hunks and join any that touch. One that overlaps another (two stored suggestions on the
 *  same text) is left out, and stays an ordinary suggestion. A joined hunk has no anchors yet:
 *  the caller gives it some (anchor()). */
export function settle(shared: string, hunks: readonly Hunk[]): { hunks: Hunk[]; gone: string[] } {
  const sorted = [...hunks].sort((x, y) => x.a - y.a || x.b - y.b)
  const out: Hunk[] = [], gone: string[] = []
  for (const h of sorted) {
    const prev = out[out.length - 1]
    if (prev && h.a < prev.b) continue
    if (prev && h.a === prev.b) {
      const joined: Hunk = { id: prev.id, a: prev.a, b: h.b, text: prev.text + h.text, base: shared.slice(prev.a, h.b), conflict: prev.conflict || h.conflict }
      if (h.id && h.id !== prev.id) { if (prev.id) gone.push(h.id); else joined.id = h.id }
      out[out.length - 1] = joined
    } else out.push(h) // the same object, so it keeps its anchors
  }
  return { hunks: out, gone }
}

/** `text` was written over `before`, which others have since changed to `after`: their change
 *  carried into `text`, or null if it touches the words `text` changes. */
export function rebase(before: string, after: string, text: string): string | null {
  // the stretches `text` leaves as they were: [from, to) in `before`, and where they are in `text`
  const kept: { from: number; to: number; at: number }[] = []
  let x = 0, y = 0
  for (const op of wordDiff(before, text)) {
    if (op.op === 'eq') { kept.push({ from: x, to: x + op.text.length, at: y }); x += op.text.length; y += op.text.length }
    else if (op.op === 'del') x += op.text.length
    else y += op.text.length
  }
  // their changes, each as [from, to) of `before` and what replaced it
  const theirs: { from: number; to: number; ins: string }[] = []
  x = 0
  for (const op of wordDiff(before, after)) {
    const last = theirs[theirs.length - 1]
    if (op.op === 'eq') { x += op.text.length; continue }
    if (op.op === 'del') { theirs.push({ from: x, to: x + op.text.length, ins: '' }); x += op.text.length }
    else if (last && last.to === x && !last.ins) last.ins = op.text
    else theirs.push({ from: x, to: x, ins: op.text })
  }
  let out = text
  for (const c of theirs.reverse()) { // from the end, so earlier places stay put
    const k = kept.find((s) => s.from <= c.from && c.to <= s.to)
    if (!k) return null
    const p = k.at + (c.from - k.from)
    out = out.slice(0, p) + c.ins + out.slice(p + (c.to - c.from))
  }
  return out
}

// ---- in the shared document -------------------------------------------------------------------

/** Where a hunk is stored: a pure insertion keeps both ends to the left, so text others type at
 *  that spot goes after it; otherwise the ends hold on to the first and last replaced characters. */
export function anchors(yt: Y.Text, a: number, b: number): { start: Uint8Array; end: Uint8Array } {
  return {
    start: Y.encodeRelativePosition(Y.createRelativePositionFromTypeIndex(yt, a, a < b ? 0 : -1)),
    end: Y.encodeRelativePosition(Y.createRelativePositionFromTypeIndex(yt, b, -1)),
  }
}

/** Give hunks that have none the anchors for where they are now. */
export function anchor(yt: Y.Text, hunks: Hunk[]) {
  for (const h of hunks) {
    if (h.s && h.e) continue
    h.s = Y.createRelativePositionFromTypeIndex(yt, h.a, h.a < h.b ? 0 : -1)
    h.e = Y.createRelativePositionFromTypeIndex(yt, h.b, -1)
  }
}

/** The hunks after the shared text changed: each moves with its anchors (one whose text has gone
 *  entirely becomes an insertion where it was), and takes in others' changes to the text it
 *  replaces where they don't touch the words it changes; otherwise it's marked as a conflict. */
export function refresh(doc: Y.Doc, yt: Y.Text, hunks: readonly Hunk[]): { hunks: Hunk[]; gone: string[] } {
  const shared = yt.toString()
  const moved: Hunk[] = []
  for (const h of hunks) {
    const s = h.s && Y.createAbsolutePositionFromRelativePosition(h.s, doc)
    const e = h.e && Y.createAbsolutePositionFromRelativePosition(h.e, doc)
    if (!s || !e || s.type !== yt || e.type !== yt) continue
    const a = Math.min(s.index, e.index), b = Math.max(s.index, e.index), base = shared.slice(a, b)
    if (a === h.a && b === h.b && base === h.base) { moved.push(h); continue }
    if (h.base === undefined || base === h.base) { moved.push({ ...h, a, b, base }); continue }
    const text = rebase(h.base, base, h.text)
    moved.push(text === null ? { ...h, a, b, base, conflict: true } : { ...h, a, b, base, text })
  }
  return settle(shared, moved)
}

/** [a, b) of a stored item in `yt`, or null if it no longer has a place there. */
export function placeOf(doc: Y.Doc, yt: Y.Text, m: Y.Map<any>): { a: number; b: number } | null {
  try {
    const s = Y.createAbsolutePositionFromRelativePosition(Y.decodeRelativePosition(m.get('start')), doc)
    const e = Y.createAbsolutePositionFromRelativePosition(Y.decodeRelativePosition(m.get('end')), doc)
    if (!s || !e || s.type !== yt || e.type !== yt) return null
    return { a: Math.min(s.index, e.index), b: Math.max(s.index, e.index) }
  } catch { return null }
}

/** This tab's open typed suggestions in `file`, as hunks, and the ids of any that others' edits
 *  have since pushed together into one (to be removed when next saved). */
export function loadHunks(doc: Y.Doc, review: Y.Map<any>, yt: Y.Text, file: string, me: string): { hunks: Hunk[]; gone: string[] } {
  const shared = yt.toString()
  const hunks: Hunk[] = []
  for (const [id, m] of review) {
    if (!(m instanceof Y.Map) || m.get('kind') !== 'suggestion' || m.get('resolved') || m.get('file') !== file || m.get('typed') !== me) continue
    const at = placeOf(doc, yt, m)
    const quote = String(m.get('quote') ?? '')
    if (at) hunks.push({ id, a: at.a, b: at.b, text: String(m.get('text') ?? ''), base: shared.slice(at.a, at.b), conflict: !!m.get('conflict') || shared.slice(at.a, at.b) !== quote })
  }
  return settle(shared, hunks)
}

export interface SaveInfo { file: string; by: string; me: string; origin: unknown }

/** Store the hunks as suggestions: new ones are added, changed ones updated, and `gone` ones
 *  removed (or, if someone has replied to one, closed). Returns the hunks with their ids. */
export function saveHunks(doc: Y.Doc, review: Y.Map<any>, yt: Y.Text, hunks: readonly Hunk[], gone: Iterable<string>, info: SaveInfo): Hunk[] {
  const shared = yt.toString()
  const out = hunks.map((h) => ({ ...h }))
  doc.transact(() => {
    for (const id of gone) {
      const m = review.get(id)
      if (!(m instanceof Y.Map) || m.get('typed') !== info.me || m.get('resolved')) continue
      const replies = m.get('replies')
      if (replies instanceof Y.Array && replies.length) m.set('resolved', true)
      else review.delete(id)
    }
    for (const h of out) {
      const quote = shared.slice(h.a, h.b)
      let m = h.id ? review.get(h.id) : undefined
      if (h.id && !(m instanceof Y.Map)) continue // removed by someone meanwhile: the caller drops it
      if (!m) {
        h.id = crypto.randomUUID()
        m = new Y.Map<any>()
        review.set(h.id, m)
        m.set('kind', 'suggestion'); m.set('file', info.file); m.set('by', info.by); m.set('at', Date.now())
        m.set('resolved', false); m.set('typed', info.me); m.set('replies', new Y.Array())
      } else {
        const at = placeOf(doc, yt, m)
        if (at && at.a === h.a && at.b === h.b && m.get('text') === h.text && m.get('quote') === quote && !!m.get('conflict') === !!h.conflict) continue // unchanged
      }
      const { start, end } = anchors(yt, h.a, h.b)
      m.set('start', start); m.set('end', end)
      if (m.get('quote') !== quote) m.set('quote', quote)
      if (m.get('text') !== h.text) m.set('text', h.text)
      if (h.conflict && !m.get('conflict')) m.set('conflict', true) // accepting it asks first
    }
  }, info.origin)
  return out
}

/** Make a suggestion's change in `yt`: only the words that differ, so text in common keeps its
 *  history and authors, and others' edits next to it are kept. [from, from + quote.length) must
 *  hold `quote`. */
export function applyChange(yt: Y.Text, from: number, quote: string, text: string) {
  let p = from
  for (const op of wordDiff(quote, text)) {
    if (op.op === 'eq') p += op.text.length
    else if (op.op === 'del') yt.delete(p, op.text.length)
    else { yt.insert(p, op.text); p += op.text.length }
  }
}
