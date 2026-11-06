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
  if (n * m > 25_000_000) // pathological sizes: replace the whole middle
    return [...a.map((t, i): Op => ({ type: 'del', a: i, b: 0, text: t })), ...b.map((t, j): Op => ({ type: 'add', a: n, b: j, text: t }))]
  const v = new Int32Array(2 * max + 2), trace: Int32Array[] = []
  for (let d = 0; d <= max; d++) {
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
