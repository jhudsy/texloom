// Line diff (Myers' O(ND) algorithm) and helpers for applying text changes to Y.Text.
import type * as Y from 'yjs'

export type Op = { type: 'eq' | 'del' | 'add'; a: number; b: number; text: string } // a/b: 0-based line in old/new

/** Line-by-line diff of two texts. */
export function diffLines(oldText: string, newText: string): Op[] {
  const a = oldText.split('\n'), b = newText.split('\n')
  // trim common prefix/suffix (cheap and makes the core small)
  let p = 0
  while (p < a.length && p < b.length && a[p] === b[p]) p++
  let s = 0
  while (s < a.length - p && s < b.length - p && a[a.length - 1 - s] === b[b.length - 1 - s]) s++
  const A = a.slice(p, a.length - s), B = b.slice(p, b.length - s)
  const ops: Op[] = []
  for (let i = 0; i < p; i++) ops.push({ type: 'eq', a: i, b: i, text: a[i] })
  ops.push(...myers(A, B).map((o) => ({ ...o, a: o.a + p, b: o.b + p })))
  for (let i = 0; i < s; i++) ops.push({ type: 'eq', a: a.length - s + i, b: b.length - s + i, text: a[a.length - s + i] })
  return ops
}

function myers(a: string[], b: string[]): Op[] {
  const n = a.length, m = b.length, max = n + m
  if (!n) return b.map((t, j) => ({ type: 'add', a: 0, b: j, text: t }))
  if (!m) return a.map((t, i) => ({ type: 'del', a: i, b: 0, text: t }))
  const replaceAll = () => [...a.map((t, i): Op => ({ type: 'del', a: i, b: 0, text: t })), ...b.map((t, j): Op => ({ type: 'add', a: n, b: j, text: t }))]
  if (n * m > 25_000_000) return replaceAll() // pathological sizes: replace the whole middle
  const v = new Int32Array(2 * max + 2), trace: Int32Array[] = []
  for (let d = 0; d <= max; d++) {
    // the trace grows by a copy of v per step: very different texts would need gigabytes
    if ((d + 1) * v.length > 16_000_000) return replaceAll()
    trace.push(v.slice())
    for (let k = -d; k <= d; k += 2) {
      let x = k === -d || (k !== d && v[max + k - 1] < v[max + k + 1]) ? v[max + k + 1] : v[max + k - 1] + 1
      let y = x - k
      while (x < n && y < m && a[x] === b[y]) { x++; y++ }
      v[max + k] = x
      if (x >= n && y >= m) return backtrack(trace, a, b, max, d)
    }
  }
  return []
}

function backtrack(trace: Int32Array[], a: string[], b: string[], max: number, dEnd: number): Op[] {
  const ops: Op[] = []
  let x = a.length, y = b.length
  for (let d = dEnd; d > 0; d--) {
    const v = trace[d], k = x - y
    const prevK = k === -d || (k !== d && v[max + k - 1] < v[max + k + 1]) ? k + 1 : k - 1
    const prevX = v[max + prevK], prevY = prevX - prevK
    while (x > prevX && y > prevY) { x--; y--; ops.push({ type: 'eq', a: x, b: y, text: a[x] }) }
    if (x === prevX) { y--; ops.push({ type: 'add', a: x, b: y, text: b[y] }) }
    else { x--; ops.push({ type: 'del', a: x, b: y, text: a[x] }) }
  }
  while (x > 0 && y > 0) { x--; y--; ops.push({ type: 'eq', a: x, b: y, text: a[x] }) }
  return ops.reverse()
}

export function diffStats(ops: Op[]) {
  return { added: ops.filter((o) => o.type === 'add').length, removed: ops.filter((o) => o.type === 'del').length }
}

/** Replace a Y.Text's content with `next` using one small edit (common prefix/suffix kept),
 *  so collaborators' cursors and concurrent edits elsewhere are preserved. */
export function setText(yt: Y.Text, next: string) {
  const old = yt.toString()
  if (old === next) return
  let p = 0
  const n = Math.min(old.length, next.length)
  while (p < n && old[p] === next[p]) p++
  let s = 0
  while (s < n - p && old[old.length - 1 - s] === next[next.length - 1 - s]) s++
  if (old.length - s - p > 0) yt.delete(p, old.length - s - p)
  if (next.length - s - p > 0) yt.insert(p, next.slice(p, next.length - s))
}

// ---- merging a synced folder's edits (foldersync.ts) -------------------------------------

type Hunk = { i1: number; i2: number; j1: number; j2: number } // lines [i1,i2) of a → [j1,j2) of b

/** Changed line ranges from a to b (texts split on '\n'). */
function hunks(a: string, b: string): Hunk[] {
  const out: Hunk[] = []
  let cur: Hunk | null = null
  let i = 0, j = 0
  for (const o of diffLines(a, b)) {
    if (o.type === 'eq') { if (cur) { out.push(cur); cur = null } i = o.a + 1; j = o.b + 1; continue }
    cur ??= { i1: i, i2: i, j1: j, j2: j }
    if (o.type === 'del') { cur.i2 = o.a + 1; i = o.a + 1 } else { cur.j2 = o.b + 1; j = o.b + 1 }
  }
  if (cur) out.push(cur)
  return out
}

const lineAt = (lines: string[], k: number) => lines[k] + (k < lines.length - 1 ? '\n' : '')
const joinLines = (lines: string[], from: number, to: number) => { let s = ''; for (let k = from; k < to; k++) s += lineAt(lines, k); return s }

/** Three-way merge by lines: `ours`' changes to `base`, on top of `theirs` (which may have
 *  moved on: collaborators typing). Where both changed the same lines, ours wins; text both
 *  inserted at one place is kept twice only if it differs. Same rules as merge3 in
 *  texloom/folder.py. */
export function merge3(base: string, theirs: string, ours: string): string {
  return merge3With(base, theirs, ours).text
}

/** As merge3, but null where both sides changed the same lines differently (git would call it a
 *  conflict), instead of keeping ours there. */
export function merge3Strict(base: string, theirs: string, ours: string): string | null {
  const m = merge3With(base, theirs, ours)
  return m.clashes ? null : m.text
}

function merge3With(base: string, theirs: string, ours: string): { text: string; clashes: number } {
  if (ours === base || theirs === ours) return { text: theirs, clashes: 0 }
  if (theirs === base) return { text: ours, clashes: 0 }
  const b = base.split('\n'), t = theirs.split('\n'), o = ours.split('\n')
  const ht = hunks(base, theirs), ho = hunks(base, ours)
  const at = (hs: Hunk[], i: number, end: boolean) => { // base line i (never inside a hunk) in the other text
    let shift = 0
    for (const h of hs) {
      if (end ? h.i2 === i : h.i1 === i) return end ? h.j2 : h.j1
      if (h.i2 <= i) shift = h.j2 - h.i2
    }
    return i + shift
  }
  const all = [...ht.map((h) => ({ ...h, side: 't' })), ...ho.map((h) => ({ ...h, side: 'o' }))]
    .sort((x, y) => x.i1 - y.i1 || x.i2 - y.i2)
  let out = '', pos = 0, k = 0, clashes = 0
  while (k < all.length) {
    let s = all[k].i1, e = all[k].i2
    const sides = new Set([all[k].side]); k++
    while (k < all.length && (all[k].i1 < e || (all[k].i1 === all[k].i2 && all[k].i1 === s && s === e))) {
      e = Math.max(e, all[k].i2); sides.add(all[k].side); k++
    }
    out += joinLines(b, pos, s)
    const mine = joinLines(o, at(ho, s, false), at(ho, e, true)), other = joinLines(t, at(ht, s, false), at(ht, e, true))
    if (sides.size > 1 && mine !== other) clashes++
    out += sides.size === 1 ? (sides.has('o') ? mine : other) : s === e && mine !== other ? other + mine : mine
    pos = e
  }
  return { text: out + joinLines(b, pos, b.length), clashes }
}

/** Turn a Y.Text from `old` (its current content) into `next` with one edit per changed run of
 *  lines, back to front, so collaborators' concurrent edits elsewhere survive. */
export function applyText(yt: Y.Text, old: string, next: string) {
  if (old === next) return
  const a = old.split('\n'), b = next.split('\n')
  const off: number[] = [0]
  for (let k = 0; k < a.length; k++) off.push(off[k] + lineAt(a, k).length)
  for (const h of hunks(old, next).reverse()) {
    const from = off[h.i1], to = off[h.i2]
    if (to > from) yt.delete(from, to - from)
    const ins = joinLines(b, h.j1, h.j2)
    if (ins) yt.insert(from, ins)
  }
}
