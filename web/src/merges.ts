// Paragraphs two people changed while apart. Yjs merges such edits character by character,
// so when both rewrote the same sentence the result can interleave their words. This notices
// it and adds a note to the Review tab, anchored to the paragraph, for someone to check.
//
// Which edits were made apart: while this browser's changes couldn't reach anyone (offline,
// still catching up after reconnecting, or sync-only with nobody else online), each place
// edited is remembered as a pair of Yjs relative positions, kept across reloads. When changes
// from others then land in a paragraph holding one of those places, the two sets of edits
// were made without seeing each other. Only the device whose edits were apart notices; the
// one that stayed online sees the note when it arrives. Once everything has been exchanged,
// the places are forgotten.
//
// Brief disconnections don't count: an edit made apart has to be at least MIN_APART old when
// the others' changes arrive. Same rules as texloom/merges.py.

import * as Y from 'yjs'
import type { Replica } from './replica'
import { addItem, allItems, mergeText, rangeOf } from './review'

export const MIN_APART = 30_000 // ms: edits apart for less than this aren't flagged
const SETTLE = 1500 // ms after the last change from others before notes are added
const FORGET_AFTER = 10_000 // ms caught up, with nothing arriving, before the places are forgotten
const MAX_PLACES = 200 // per file before nearby places are merged into one
const LONG = 3000 // characters: a "paragraph" longer than this is narrowed to the lines changed

interface Place { s: Y.RelativePosition; e: Y.RelativePosition; at: number }
type Stored = Record<string, [string, string, number][]>

const enc = (p: Y.RelativePosition) => btoa(String.fromCharCode(...Y.encodeRelativePosition(p)))
const dec = (s: string) => Y.decodeRelativePosition(Uint8Array.from(atob(s), (c) => c.charCodeAt(0)))

/** The paragraph around [from, to) in `text`: between blank lines, or just the lines if it's very long. */
export function paragraphAt(text: string, from: number, to: number): [number, number] {
  const blank = /\n[ \t]*\n/g
  let a = 0, b = text.length
  for (let m; (m = blank.exec(text));) {
    const end = m.index + m[0].length
    if (end <= from) a = end
    else if (m.index >= to) { b = m.index; break }
  }
  if (b - a > LONG) {
    a = text.lastIndexOf('\n', Math.max(from - 1, 0)) + 1
    const nl = text.indexOf('\n', to)
    b = nl < 0 ? text.length : nl
  }
  return [a, b]
}

/** Ranges a text event changed, in the text as it is now: [from, to), empty where text was deleted. */
export function changedRanges(delta: { retain?: number; insert?: unknown; delete?: number }[]): [number, number][] {
  const out: [number, number][] = []
  let i = 0
  for (const d of delta) {
    if (d.retain) i += d.retain
    else if (d.insert !== undefined) { const n = typeof d.insert === 'string' ? d.insert.length : 1; out.push([i, i + n]); i += n }
    else if (d.delete) out.push([i, i])
  }
  return out
}

export function watchMerges(r: Replica, myName: () => string) {
  const key = `texloom-apart-${r.keys.project}`
  const places = new Map<string, Place[]>()
  try {
    const stored = JSON.parse(localStorage.getItem(key) ?? '{}') as Stored
    for (const [file, ps] of Object.entries(stored)) places.set(file, ps.map(([s, e, at]) => ({ s: dec(s), e: dec(e), at })))
  } catch { /* none kept */ }
  let saveTimer: ReturnType<typeof setTimeout> | undefined
  const save = () => {
    clearTimeout(saveTimer)
    saveTimer = setTimeout(() => {
      try {
        if (!places.size) { localStorage.removeItem(key); return }
        const out: Stored = {}
        for (const [file, ps] of places) out[file] = ps.map((p) => [enc(p.s), enc(p.e), p.at])
        localStorage.setItem(key, JSON.stringify(out))
      } catch { /* storage full or blocked: kept in memory only */ }
    }, 1000)
  }
  const resolve = (yt: Y.Text, p: Place): [number, number] | null => {
    const a = Y.createAbsolutePositionFromRelativePosition(p.s, r.doc), b = Y.createAbsolutePositionFromRelativePosition(p.e, r.doc)
    if (!a || !b || a.type !== yt || b.type !== yt) return null
    return [Math.min(a.index, b.index), Math.max(a.index, b.index)]
  }
  /** Too many places in a file: merge the ones within a paragraph's reach of each other. */
  const tidy = (file: string, yt: Y.Text) => {
    const ps = places.get(file)!
    const spans = ps.map((p) => ({ at: p.at, r: resolve(yt, p) })).filter((x) => x.r).sort((x, y) => x.r![0] - y.r![0])
    const merged: { a: number; b: number; at: number }[] = []
    for (const { r: [a, b], at } of spans as { r: [number, number]; at: number }[]) {
      const last = merged[merged.length - 1]
      if (last && a - last.b < 200) { last.b = Math.max(last.b, b); last.at = Math.min(last.at, at) } else merged.push({ a, b, at })
    }
    places.set(file, merged.map(({ a, b, at }) => ({
      s: Y.createRelativePositionFromTypeIndex(yt, a), e: Y.createRelativePositionFromTypeIndex(yt, b, -1), at })))
  }

  // places found to need a note, kept as positions until the others' changes have settled
  let found: { file: string; s: Y.RelativePosition; e: Y.RelativePosition; who: Set<string> }[] = []
  let noteTimer: ReturnType<typeof setTimeout> | undefined
  let lastRemote = 0, caughtUpSince = 0

  r.files.observeDeep((events, tx) => {
    if (!r.keys.writable || r.deletedAt) return
    const local = r.isLocal(tx.origin), remote = r.isRemote(tx.origin)
    if (remote) lastRemote = Date.now()
    if (local && !r.apart) return
    if (!local && !remote) return
    for (const ev of events) {
      const yt = ev.target
      if (!(yt instanceof Y.Text)) continue
      const file = (yt as any)._item?.parentSub as string | undefined
      if (typeof file !== 'string' || r.files.get(file) !== yt) continue
      const ranges = changedRanges(ev.delta as any)
      if (!ranges.length) continue
      if (local) { // edited apart: remember where
        const ps = places.get(file) ?? []
        const now = Date.now()
        for (const [a, b] of ranges) ps.push({ s: Y.createRelativePositionFromTypeIndex(yt, a), e: Y.createRelativePositionFromTypeIndex(yt, b, -1), at: now })
        places.set(file, ps)
        if (ps.length > MAX_PLACES) tidy(file, yt)
        save()
        continue
      }
      const mine = places.get(file)
      if (!mine?.length) continue
      const text = yt.toString(), old = Date.now() - MIN_APART
      const spans = mine.filter((p) => p.at <= old).map((p) => resolve(yt, p)).filter((x): x is [number, number] => !!x)
      if (!spans.length) continue
      const who = new Set<string>()
      for (const [client, clock] of tx.afterState) {
        if (clock > (tx.beforeState.get(client) ?? 0) && client !== r.doc.clientID) who.add(r.clients.get(String(client)) ?? '')
      }
      for (const [a, b] of ranges) {
        const [pa, pb] = paragraphAt(text, a, b)
        if (!spans.some(([s, e]) => s <= pb && e >= pa)) continue
        found.push({ file, s: Y.createRelativePositionFromTypeIndex(yt, pa), e: Y.createRelativePositionFromTypeIndex(yt, pb, -1), who })
      }
    }
    if (found.length) { clearTimeout(noteTimer); noteTimer = setTimeout(addNotes, SETTLE) }
  })

  function addNotes() {
    if (Date.now() - lastRemote < SETTLE) { noteTimer = setTimeout(addNotes, SETTLE); return } // still arriving
    const todo = found
    found = []
    // one note per paragraph, unless one is already open there
    const byFile = new Map<string, { a: number; b: number; who: Set<string> }[]>()
    for (const f of todo) {
      const yt = r.files.get(f.file)
      const a = Y.createAbsolutePositionFromRelativePosition(f.s, r.doc), b = Y.createAbsolutePositionFromRelativePosition(f.e, r.doc)
      if (!yt || !a || !b || a.type !== yt || b.type !== yt) continue
      const text = yt.toString()
      const [pa, pb] = paragraphAt(text, Math.min(a.index, b.index), Math.max(a.index, b.index)) // the paragraph as it reads now
      const list = byFile.get(f.file) ?? []
      const same = list.find((x) => x.a < pb && x.b > pa)
      if (same) { same.a = Math.min(same.a, pa); same.b = Math.max(same.b, pb); f.who.forEach((w) => same.who.add(w)) } else list.push({ a: pa, b: pb, who: new Set(f.who) })
      byFile.set(f.file, list)
    }
    const open = allItems(r).filter((i) => i.merge && !i.resolved)
    r.doc.transact(() => {
      for (const [file, list] of byFile) {
        for (const { a, b, who } of list) {
          if (b <= a) continue
          if (open.some((i) => i.file === file && (() => { const g = rangeOf(r, i.id); return !!g && g.from < b && g.to > a })())) continue
          const names = [...who].filter(Boolean).sort()
          const id = addItem(r, 'comment', file, a, b, mergeText(myName(), names), myName())
          if (id) (r.review.get(id) as Y.Map<any>).set('merge', names)
        }
      }
    })
  }

  // Notes from the desktop app come without anchors, only the paragraph's text (texloom/merges.py:
  // pycrdt can't be trusted to place anchors in non-ASCII text). Anchor each where that text is,
  // if it's there exactly once.
  const anchorQuoted = () => {
    if (!r.keys.writable || r.deletedAt) return
    const todo: [Y.Map<any>, Y.Text, number, number][] = []
    for (const m of r.review.values()) {
      if (!(m instanceof Y.Map) || m.get('start') || m.get('resolved')) continue
      const q = m.get('quote'), yt = r.files.get(String(m.get('file') ?? ''))
      if (typeof q !== 'string' || !q || !(yt instanceof Y.Text)) continue
      const s = yt.toString(), i = s.indexOf(q)
      if (i >= 0 && s.indexOf(q, i + 1) < 0) todo.push([m, yt, i, i + q.length])
    }
    if (todo.length) r.doc.transact(() => {
      for (const [m, yt, a, b] of todo) {
        m.set('start', Y.encodeRelativePosition(Y.createRelativePositionFromTypeIndex(yt, a)))
        m.set('end', Y.encodeRelativePosition(Y.createRelativePositionFromTypeIndex(yt, b, -1)))
      }
    })
  }
  r.review.observe((_e, tx) => { if (r.isRemote(tx.origin)) setTimeout(anchorQuoted) })
  setTimeout(anchorQuoted)

  // forget the places once everything has been exchanged for a while
  setInterval(() => {
    if (!places.size) return
    if (!r.exchanged || Date.now() - lastRemote < FORGET_AFTER) { caughtUpSince = 0; return }
    caughtUpSince ||= Date.now()
    if (Date.now() - caughtUpSince >= FORGET_AFTER && !found.length) { places.clear(); save() }
  }, 2000)
}
